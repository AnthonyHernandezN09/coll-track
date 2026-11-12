#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from threading import Event, Thread
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from brandfetch import lookup, university_domain

PORT = 46387
LIMIT = 2 * 1024 * 1024
MIN_INTERVAL, MAX_INTERVAL = 1, 10080
CHECK_LEASE_MS = 45000


def known_school(db, host):
    if db.execute("SELECT 1 FROM schools WHERE host=?", (host,)).fetchone():
        return True
    return any(urlsplit(source).hostname == host for (source,) in db.execute("SELECT source FROM snapshots"))


def school_snapshots(db):
    saved = {host: (name, portal) for host, name, portal in db.execute("SELECT host, name, portal FROM schools")}
    seen = set()
    for (raw,) in db.execute("SELECT record FROM snapshots ORDER BY imported_at DESC"):
        record = json.loads(raw)
        host = urlsplit(record["source"]).hostname
        seen.add(host)
        if host in saved:
            record["school"], record["portal"] = saved[host]
        yield record
    for host, (name, portal) in saved.items():
        if host not in seen:
            yield {"school": name, "portal": portal, "source": f"https://{host}/",
                   "importedAt": None, "checklists": []}


def read_school_order(db):
    row = db.execute("SELECT hosts FROM school_order WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def write_school_order(db, hosts):
    db.execute("INSERT OR REPLACE INTO school_order VALUES (1, ?)", (json.dumps(hosts),))


def append_school_to_order(db, host):
    order = read_school_order(db)
    if order is not None and host and host not in order:
        write_school_order(db, [*order, host])


def checklist_items(record):
    items = {}
    for section in record["checklists"]:
        for item in section["items"]:
            name = (section["kind"], section["title"].casefold(), item["item"].casefold())
            occurrence = 1
            while (*name, occurrence) in items:
                occurrence += 1
            items[(*name, occurrence)] = item
    return items


def checklist_changes(previous, current):
    before, after = checklist_items(previous), checklist_items(current)
    for identity in dict.fromkeys([*after, *before]):
        old, item = before.get(identity), after.get(identity)
        if old == item or (old and item and item["status"] == "unknown" and old.get("status") != "unknown"):
            continue
        yield {"item": (item or old)["item"], "before": old, "after": item}


def add_update(db, data, details):
    change = {"school": data["school"], "source": data["source"], "at": data["importedAt"], **details}
    db.execute("INSERT INTO updates (record) VALUES (?)", (json.dumps(change),))


def save_snapshot(db, data, *, automatic=False):
    source, stamp = data["source"], data["importedAt"]
    previous = db.execute("SELECT imported_at, record FROM snapshots WHERE source=?", (source,)).fetchone()
    if previous and previous[0] >= stamp:
        return []
    old_record = json.loads(previous[1]) if previous else None
    if automatic and old_record and all(old_record.get(key) == data.get(key) for key in ("checklists", "unreadable")):
        return []
    changes = list(checklist_changes(old_record, data)) if old_record else []
    if not automatic:
        for change in changes:
            add_update(db, data, change)
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (source, stamp, json.dumps(data)))
    append_school_to_order(db, urlsplit(source).hostname)
    return changes


def save_scan(db, data, tracker):
    source, page = tracker["source"], data["page"]
    record = {key: data[key] for key in ("importedAt", "checklists", "unreadable")}
    record.update(source=source, school=tracker["school"])
    previous = db.execute("SELECT page FROM portal_snapshots WHERE source=? ORDER BY id DESC LIMIT 1",
                          (source,)).fetchone()
    before = json.loads(previous[0]) if previous else page
    if previous and before == page:
        page_change = None
    elif previous:
        removed = Counter(before) - Counter(page)
        added = Counter(page) - Counter(before)
        page_change = {"removed": list(removed.elements()), "added": list(added.elements())}
    else:
        page_change = None
    readable = not record["unreadable"] and all(not section.get("skippedRows") for section in record["checklists"])
    changes = save_snapshot(db, record, automatic=True) if readable else []
    if not previous or page_change is not None or changes:
        db.execute("INSERT INTO portal_snapshots (source, imported_at, page, checklists) VALUES (?, ?, ?, ?)",
                   (source, record["importedAt"], json.dumps(page), json.dumps(record["checklists"])))
    if page_change is not None or changes:
        add_update(db, record, {"item": "Portal updated", "changes": changes, "page": page_change})
    return "checked" if readable else "unreadable"


def check_interval(value):
    return type(value) is int and MIN_INTERVAL <= value <= MAX_INTERVAL


def initialize(directory):
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = directory / "vault-key"
    try:
        with key_path.open("xb") as file:
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    with closing(sqlite3.connect(directory / "coll-track.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY, revision TEXT NOT NULL, sealed TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            source TEXT PRIMARY KEY, imported_at TEXT NOT NULL, record TEXT NOT NULL
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, record TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)")
        db.execute("""CREATE TABLE IF NOT EXISTS trackers (
            source TEXT PRIMARY KEY, url TEXT NOT NULL, school TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 60,
            enabled INTEGER NOT NULL DEFAULT 1, checked INTEGER, result TEXT
        )""")
        if "checking_until" not in {row[1] for row in db.execute("PRAGMA table_info(trackers)")}:
            db.execute("ALTER TABLE trackers ADD COLUMN checking_until INTEGER")
        if "profile_id" not in {row[1] for row in db.execute("PRAGMA table_info(trackers)")}:
            db.execute("ALTER TABLE trackers ADD COLUMN profile_id TEXT")
        db.execute("CREATE TABLE IF NOT EXISTS tracker_defaults (id INTEGER PRIMARY KEY, interval INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO tracker_defaults VALUES (1, 60)")
        db.execute("""CREATE TABLE IF NOT EXISTS portal_snapshots (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, imported_at TEXT NOT NULL, page TEXT NOT NULL,
            checklists TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS portal_snapshots_source ON portal_snapshots (source, id)")
        db.execute("CREATE TABLE IF NOT EXISTS branding_settings (id INTEGER PRIMARY KEY, mode TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS branding_selection (id INTEGER PRIMARY KEY, hosts TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS school_order (id INTEGER PRIMARY KEY, hosts TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS schools (host TEXT PRIMARY KEY, name TEXT NOT NULL, portal TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS school_branding (host TEXT PRIMARY KEY, color TEXT, logo TEXT)")
    return key_path.read_bytes()


def crypt(key, mode, data):
    return subprocess.run(
        [str(Path(__file__).with_name("vault-crypto")), mode],
        input=key + data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=True, timeout=5
    ).stdout


def load_profiles(db, key):
    row = db.execute("SELECT revision, sealed FROM profiles WHERE id=1").fetchone()
    if not row:
        return None, []
    return row[0], json.loads(crypt(key, "open", base64.b64decode(row[1])))


def monitor_portals(directory, key, stop, monitor):
    path = directory / "coll-track.sqlite3"
    while not stop.is_set():
        try:
            now = int(time.time() * 1000)
            with closing(sqlite3.connect(path)) as db, db:
                db.row_factory = sqlite3.Row
                tracker = db.execute("""UPDATE trackers SET checking_until=?
                    WHERE source=(SELECT source FROM trackers WHERE enabled=1
                        AND COALESCE(checking_until, 0) <= ?
                        AND COALESCE(checked, 0) + interval * 60000 <= ?
                        ORDER BY COALESCE(checked, 0), source LIMIT 1)
                    RETURNING *""", (now + CHECK_LEASE_MS, now, now)).fetchone()
            if not tracker:
                stop.wait(2)
                continue
            try:
                with closing(sqlite3.connect(path)) as db:
                    _, profiles = load_profiles(db, key)
                status, result = monitor.scan(tracker, profiles)
            except Exception:
                status, result = "error", None
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                claimed = db.execute("SELECT 1 FROM trackers WHERE source=? AND checking_until=? AND enabled=1",
                                     (tracker["source"], tracker["checking_until"])).fetchone()
                if claimed:
                    if result is not None:
                        status = save_scan(db, result, tracker)
                    db.execute("UPDATE trackers SET checked=?, result=?, checking_until=NULL WHERE source=?",
                               (int(time.time() * 1000), status, tracker["source"]))
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            stop.wait(2)


def handler_for(directory, vault_key, port):
    assets = {
        "/": ("index.html", "text/html"),
        "/dashboard.js": ("dashboard.js", "text/javascript"),
        "/dashboard.css": ("dashboard.css", "text/css"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def origin(self):
            origin = self.headers.get("Origin", "")
            return origin if re.fullmatch(r"chrome-extension://[a-p]{32}", origin) else None

        def reply(self, status, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_bytes(status, data, "application/json")

        def send_bytes(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type if content_type.startswith("image/") else content_type + "; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' https://cdn.brandfetch.io data:; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin" if not self.origin() else "cross-origin")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if origin := self.origin():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            if data:
                self.wfile.write(data)

        def valid_source(self):
            return self.headers.get("Host") == f"127.0.0.1:{port}" and (
                self.headers.get("Origin", "") in ("", f"http://127.0.0.1:{port}") or self.origin()
            )

        def do_OPTIONS(self):
            if not self.valid_source() or not self.origin():
                return self.reply(403)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.origin())
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Coll-Track")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def extension_api(self):
            return self.valid_source() and self.headers.get("X-Coll-Track") == "extension"

        def do_GET(self):
            if not self.valid_source():
                return self.reply(403)
            if self.path in assets:
                filename, content_type = assets[self.path]
                data = (Path(__file__).parent / filename).read_bytes()
                return self.send_bytes(200, data, content_type)
            if self.path == "/profiles" and not self.extension_api():
                return self.reply(403)
            with closing(sqlite3.connect(directory / "coll-track.sqlite3")) as db:
                if self.path in ("/profiles", "/trackers/profiles"):
                    try:
                        revision, profiles = load_profiles(db, vault_key)
                    except (ValueError, subprocess.SubprocessError):
                        return self.reply(500, {"error": "Could not read the saved profiles."})
                    if self.path == "/trackers/profiles":
                        return self.reply(200, [{name: profile[name] for name in ("id", "name", "hosts")}
                                                for profile in profiles])
                    return self.reply(200, {"revision": revision, "profiles": profiles})
                if self.path == "/branding/settings":
                    row = db.execute("SELECT mode FROM branding_settings WHERE id=1").fetchone()
                    configured = bool(os.environ.get("BRANDFETCH_API_KEY")) or (
                        (directory / "brandfetch-key").is_file() and (directory / "brandfetch-key").stat().st_size > 0
                    )
                    selection = db.execute("SELECT hosts FROM branding_selection WHERE id=1").fetchone()
                    return self.reply(200, {"mode": row[0] if row else None, "configured": configured,
                                            "hosts": json.loads(selection[0]) if selection else None})
                if urlsplit(self.path).path == "/branding":
                    host = parse_qs(urlsplit(self.path).query).get("host", [""])[0]
                    if not known_school(db, host):
                        return self.reply(404)
                    mode = db.execute("SELECT mode FROM branding_settings WHERE id=1").fetchone()
                    manual = db.execute("SELECT color, logo FROM school_branding WHERE host=?", (host,)).fetchone()
                    domain = university_domain(host)
                    selection = db.execute("SELECT hosts FROM branding_selection WHERE id=1").fetchone()
                    selected = not selection or host in json.loads(selection[0])
                    brand = lookup(directory, domain) if domain and selected and mode and mode[0] == "automatic" else {}
                    custom = dict(zip(("color", "logo"), manual)) if manual else {}
                    brand.update({name: value for name, value in custom.items() if value})
                    return self.reply(200, {**brand, "custom": custom})
                if self.path == "/schools/order":
                    return self.reply(200, {"hosts": read_school_order(db)})
                if self.path == "/snapshots":
                    return self.reply(200, list(school_snapshots(db)))
                if self.path == "/updates":
                    rows = db.execute("SELECT id, record, read FROM updates ORDER BY id DESC")
                    return self.reply(200, [dict(json.loads(record), id=id_, read=bool(read)) for id_, record, read in rows])
                if self.path == "/trackers/defaults":
                    return self.reply(200, {"interval": db.execute("SELECT interval FROM tracker_defaults WHERE id=1").fetchone()[0]})
                if self.path == "/trackers":
                    db.row_factory = sqlite3.Row
                    trackers = [dict(row) for row in db.execute("SELECT * FROM trackers ORDER BY school")]
                    if self.extension_api():
                        for tracker in trackers:
                            tracker["enabled"] = 0
                    return self.reply(200, trackers)
            return self.reply(404)

        def do_PUT(self):
            dashboard_write = self.path in ("/updates/read", "/trackers/settings", "/trackers/defaults", "/trackers/register", "/branding/settings", "/branding/school", "/schools", "/schools/order") and (
                self.valid_source() and self.headers.get("Origin") == f"http://127.0.0.1:{port}"
                and self.headers.get("Content-Type") == "application/json"
            )
            if not (dashboard_write or self.extension_api()):
                return self.reply(403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= LIMIT:
                    return self.reply(413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    return self.reply(400)
                with closing(sqlite3.connect(directory / "coll-track.sqlite3")) as db, db:
                    if self.path == "/schools/order":
                        hosts, initialize_only = data.get("hosts"), data.get("initialize", False)
                        if (not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts)
                            or type(initialize_only) is not bool):
                            return self.reply(400)
                        db.execute("BEGIN IMMEDIATE")
                        saved = read_school_order(db)
                        if not initialize_only or saved is None:
                            known = dict.fromkeys(urlsplit(record["source"]).hostname for record in school_snapshots(db))
                            preferred = [host for host in [*hosts, *(saved or [])] if host in known]
                            saved = list(dict.fromkeys([*preferred, *known]))
                            write_school_order(db, saved)
                        db.commit()
                        return self.reply(200, {"hosts": saved})
                    elif self.path == "/schools":
                        name, address, expected = data.get("name"), data.get("portal"), data.get("host")
                        if not isinstance(name, str) or not 0 < len(name.strip()) <= 120:
                            return self.reply(400, {"error": "Enter a school name up to 120 characters."})
                        if not isinstance(address, str) or not re.fullmatch(r"https://[^#\s\\]{1,2048}", address):
                            return self.reply(400, {"error": "Enter an HTTPS portal address."})
                        portal = urlsplit(address)
                        host = portal.hostname
                        if (not host or not university_domain(host) or portal.netloc.lower() != host
                            or (expected is not None and expected != host)):
                            return self.reply(400, {"error": "Enter an HTTPS address on the school’s portal host."})
                        db.execute("BEGIN IMMEDIATE")
                        if expected is None and known_school(db, host):
                            return self.reply(409, {"error": "This portal is already in My universities."})
                        if expected is not None and not known_school(db, host):
                            return self.reply(404)
                        db.execute("INSERT OR REPLACE INTO schools VALUES (?, ?, ?)", (host, name.strip(), address))
                        append_school_to_order(db, host)
                        db.commit()
                        return self.reply(200, {"source": f"https://{host}/"})
                    elif self.path == "/branding/settings":
                        mode, key = data.get("mode"), data.get("key")
                        if mode not in ("automatic", "manual") or (key and (
                            mode != "automatic" or not isinstance(key, str) or
                            not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", key)
                        )):
                            return self.reply(400, {"error": "Enter a valid Brandfetch API key."})
                        hosts = data.get("hosts")
                        if hosts is not None and (not isinstance(hosts, list) or len(hosts) > 2000 or
                            any(not isinstance(host, str) or not university_domain(host) for host in hosts)):
                            return self.reply(400, {"error": "Choose valid school addresses."})
                        key_path = directory / "brandfetch-key"
                        if mode == "automatic" and not (key or os.environ.get("BRANDFETCH_API_KEY") or key_path.is_file()):
                            return self.reply(400, {"error": "Enter a Brandfetch API key first."})
                        if key:
                            replacement = directory / ("brandfetch-key-" + secrets.token_hex(8))
                            try:
                                replacement.write_text(key + "\n")
                                replacement.chmod(0o600)
                                replacement.replace(key_path)
                            finally:
                                replacement.unlink(missing_ok=True)
                        db.execute("INSERT OR REPLACE INTO branding_settings VALUES (1, ?)", (mode,))
                        if hosts is not None:
                            db.execute("INSERT OR REPLACE INTO branding_selection VALUES (1, ?)",
                                       (json.dumps(list(dict.fromkeys(hosts))),))
                    elif self.path == "/branding/school":
                        host, color = data.get("host"), data.get("color")
                        if not isinstance(host, str) or not known_school(db, host):
                            return self.reply(404)
                        if color and (not isinstance(color, str) or not re.fullmatch(r"#[A-Fa-f0-9]{6}", color)):
                            return self.reply(400, {"error": "Enter a six-digit hex color."})
                        existing = db.execute("SELECT logo FROM school_branding WHERE host=?", (host,)).fetchone()
                        logo = data.get("logo", existing[0] if existing else None)
                        if logo:
                            image = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", logo)
                            if not image:
                                return self.reply(400, {"error": "Choose a PNG, JPEG, or WebP image."})
                            raw = base64.b64decode(image[2], validate=True)
                            signatures = {"png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8", "webp": b"RIFF"}
                            if (len(raw) > 512 * 1024 or not raw.startswith(signatures[image[1]]) or
                                (image[1] == "webp" and raw[8:12] != b"WEBP")):
                                return self.reply(400, {"error": "Choose a valid image under 512 KB."})
                        db.execute("INSERT OR REPLACE INTO school_branding VALUES (?, ?, ?)",
                                   (host, color or None, logo or None))
                    elif self.path == "/profiles":
                        profiles = data.get("profiles")
                        if not isinstance(profiles, list) or any(
                            not isinstance(profile, dict) or not all(
                                isinstance(profile.get(field), str) and profile[field]
                                for field in ("id", "name", "email", "password")
                            ) or not isinstance(profile.get("hosts"), list) or not profile["hosts"]
                            or not all(isinstance(host, str) for host in profile["hosts"])
                            for profile in profiles
                        ):
                            return self.reply(400)
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute("SELECT revision FROM profiles WHERE id=1").fetchone()
                        if (row[0] if row else None) != data.get("previous"):
                            return self.reply(409, {"error": "Profiles changed. Reopen settings."})
                        revision = secrets.token_hex(16)
                        sealed = base64.b64encode(crypt(vault_key, "seal", json.dumps(profiles).encode())).decode()
                        db.execute("INSERT OR REPLACE INTO profiles VALUES (1, ?, ?)", (revision, sealed))
                        db.commit()
                        return self.reply(200, {"revision": revision})
                    elif self.path == "/snapshots":
                        source, stamp = data.get("source"), data.get("importedAt")
                        if not isinstance(source, str) or not re.fullmatch(r"https://[^?#\s]+", source) or not isinstance(
                            stamp, str
                        ) or not isinstance(data.get("checklists"), list):
                            return self.reply(400)
                        db.execute("BEGIN IMMEDIATE")
                        save_snapshot(db, data)
                    elif self.path == "/updates/read":
                        id_ = data.get("id")
                        read = data.get("read")
                        if type(id_) is not int or type(read) is not bool:
                            return self.reply(400)
                        db.execute("UPDATE updates SET read=? WHERE id=?", (int(read), id_))
                    elif self.path == "/trackers/register":
                        url = urlsplit(data.get("url", ""))
                        source = data.get("source")
                        school = data.get("school")
                        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.port
                            or url.path.startswith("/account/") or url.fragment
                            or source != f"https://{url.netloc}{url.path}" or not isinstance(school, str)
                            or not known_school(db, url.hostname)):
                            return self.reply(400)
                        db.execute("""INSERT INTO trackers (source, url, school, interval)
                            VALUES (?, ?, ?, (SELECT interval FROM tracker_defaults WHERE id=1))
                            ON CONFLICT(source) DO UPDATE SET url=excluded.url, school=excluded.school,
                            enabled=1, checked=NULL, result=NULL, checking_until=NULL""", (source, url.geturl(), school))
                    elif self.path == "/trackers/defaults":
                        interval = data.get("interval")
                        if not check_interval(interval):
                            return self.reply(400)
                        db.execute("UPDATE tracker_defaults SET interval=? WHERE id=1", (interval,))
                        db.execute("UPDATE trackers SET interval=?", (interval,))
                    elif self.path == "/trackers/settings":
                        interval, enabled = data.get("interval"), data.get("enabled")
                        if not check_interval(interval) or type(enabled) is not bool:
                            return self.reply(400)
                        source = data.get("source")
                        profile_id = data.get("profileId")
                        if profile_id is not None:
                            _, profiles = load_profiles(db, vault_key)
                            host = urlsplit(source).hostname
                            if not any(profile["id"] == profile_id and host in profile["hosts"] for profile in profiles):
                                return self.reply(400, {"error": "Choose a saved login assigned to this portal."})
                        db.execute("""UPDATE trackers SET interval=?, enabled=?, profile_id=?,
                            checked=CASE WHEN enabled=0 OR profile_id IS NOT ? THEN NULL ELSE checked END,
                            result=CASE WHEN enabled=0 OR profile_id IS NOT ? THEN NULL ELSE result END,
                            checking_until=CASE WHEN ?=0 OR profile_id IS NOT ? THEN NULL ELSE checking_until END
                            WHERE source=?""",
                            (interval, int(enabled), profile_id, profile_id, profile_id, int(enabled), profile_id, source))
                    else:
                        return self.reply(404)
                return self.reply(200, {})
            except subprocess.SubprocessError:
                return self.reply(500)
            except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                return self.reply(400)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "Library/Application Support/Coll-Track")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    key = initialize(args.data_dir)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.data_dir, key, args.port)) as server:
        stop = Event()
        if args.monitor:
            from portal_monitor import PortalMonitor
            Thread(target=monitor_portals, args=(args.data_dir, key, stop, PortalMonitor()), daemon=True).start()
        try:
            server.serve_forever()
        finally:
            stop.set()
