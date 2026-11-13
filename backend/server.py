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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

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


def status_address(address):
    url = urlsplit(address)
    if url.scheme != "https" or not url.hostname or url.netloc.lower() != url.hostname:
        raise ValueError("Enter an HTTPS portal address.")
    if url.path.startswith("/account/"):
        next_url = urlsplit(urljoin(address, parse_qs(url.query).get("r", ["/apply/status"])[0]))
        if next_url.scheme != url.scheme or next_url.netloc != url.netloc or next_url.path.startswith("/account/"):
            raise ValueError("Enter the application status page address.")
        query = urlencode([(key, value) for key, values in parse_qs(next_url.query).items()
                           for value in values[:1] if key in ("cmd", "tab") and re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", value)])
        url = next_url._replace(query=query)
    return url._replace(path=url.path or "/", fragment="").geturl()


def shared_schools(db):
    records = {}
    aliases = dict(db.execute("SELECT host, alias FROM portal_aliases"))
    for record in school_snapshots(db):
        host = urlsplit(record["source"]).hostname
        if host in records:
            continue
        try:
            address = status_address(record.get("portal") or record["source"])
        except ValueError:
            address = record["source"]
        records[host] = dict(record, id=aliases.get(host, host), host=host, portal=address)
    order = read_school_order(db) or []
    return [records[host] for host in dict.fromkeys([*order, *records]) if host in records]


def read_school_order(db):
    row = db.execute("SELECT hosts FROM school_order WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def write_school_order(db, hosts):
    db.execute("INSERT OR REPLACE INTO school_order VALUES (1, ?)", (json.dumps(hosts),))


def append_school_to_order(db, host):
    order = read_school_order(db)
    if order is not None and host and host not in order:
        write_school_order(db, [*order, host])


def checklist_entries(record):
    occurrences = Counter()
    for section in record["checklists"]:
        for index, item in enumerate(section["items"]):
            name = (section["kind"], section["title"].casefold(), item["item"].casefold())
            occurrences[name] += 1
            yield (*name, occurrences[name]), section["items"], index


def checklist_items(record):
    return {identity: items[index] for identity, items, index in checklist_entries(record)}


def checklist_readable(record):
    return bool(record["checklists"]) and not record.get("unreadable") and all(
        not section.get("skippedRows") for section in record["checklists"]
    )


def checklist_changes(previous, current):
    before, after = checklist_items(previous), checklist_items(current)
    for identity in dict.fromkeys([*after, *before]):
        old, item = before.get(identity), after.get(identity)
        if old and item and (
            (old.get("status"), old.get("dateText") or "") == (item.get("status"), item.get("dateText") or "")
            or (item.get("status") == "unknown" and old.get("status") != "unknown")
        ):
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
    if old_record:
        known_items = checklist_items(old_record)
        for identity, items, index in checklist_entries(data):
            previous_item = known_items.get(identity)
            if previous_item and previous_item["status"] != "unknown" and items[index]["status"] == "unknown":
                items[index] = previous_item
        if automatic and all(old_record.get(key) == data.get(key) for key in ("checklists", "unreadable")):
            return []
    changes = list(checklist_changes(old_record, data)) if old_record else []
    if not automatic:
        for change in changes:
            add_update(db, data, change)
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (source, stamp, json.dumps(data)))
    append_school_to_order(db, urlsplit(source).hostname)
    return changes


def save_scan(db, data, tracker):
    source = tracker["source"]
    record = {key: data[key] for key in ("importedAt", "checklists", "unreadable")}
    record.update(source=source, school=tracker["school"])
    if not checklist_readable(record):
        db.execute("UPDATE trackers SET pending_page=NULL WHERE source=?", (source,))
        return "unreadable"

    page = Counter(data["page"])
    previous = db.execute("""SELECT page FROM portal_snapshots
        WHERE source=? AND trusted=1 ORDER BY id DESC LIMIT 1""", (source,)).fetchone()
    before = Counter(json.loads(previous[0])) if previous else page
    removed, added = before - page, page - before
    page_changed = bool(removed or added)
    pending = tracker["pending_page"]
    confirmed = page_changed and pending is not None and Counter(json.loads(pending)) == page
    if page_changed and not confirmed:
        db.execute("UPDATE trackers SET pending_page=? WHERE source=?", (json.dumps(data["page"]), source))
    else:
        db.execute("UPDATE trackers SET pending_page=NULL WHERE source=?", (source,))

    changes = save_snapshot(db, record, automatic=True)
    page_change = None
    if confirmed:
        page_change = {"removed": list(removed.elements()), "added": list(added.elements())}
    if not previous or confirmed:
        db.execute("""INSERT INTO portal_snapshots (source, imported_at, page, checklists, trusted)
            VALUES (?, ?, ?, ?, 1)""",
            (source, record["importedAt"], json.dumps(data["page"]), json.dumps(record["checklists"])))
    if changes or page_change:
        add_update(db, record, {"item": "Portal updated", "changes": changes, "page": page_change})
    return "checked"


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
        if "trusted" not in {row[1] for row in db.execute("PRAGMA table_info(portal_snapshots)")}:
            db.execute("ALTER TABLE portal_snapshots ADD COLUMN trusted INTEGER NOT NULL DEFAULT 0")
        if "pending_page" not in {row[1] for row in db.execute("PRAGMA table_info(trackers)")}:
            db.execute("ALTER TABLE trackers ADD COLUMN pending_page TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS portal_snapshots_source ON portal_snapshots (source, id)")
        db.execute("CREATE TABLE IF NOT EXISTS branding_settings (id INTEGER PRIMARY KEY, mode TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS branding_selection (id INTEGER PRIMARY KEY, hosts TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS school_order (id INTEGER PRIMARY KEY, hosts TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS schools (host TEXT PRIMARY KEY, name TEXT NOT NULL, portal TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS new_school_branding (host TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS school_branding (host TEXT PRIMARY KEY, color TEXT, logo TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS portal_aliases (host TEXT PRIMARY KEY, alias TEXT UNIQUE COLLATE NOCASE)")
        if "requested" not in {row[1] for row in db.execute("PRAGMA table_info(trackers)")}:
            db.execute("ALTER TABLE trackers ADD COLUMN requested INTEGER")
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


DEFAULT_LOGIN_ID = "coll-track-default"


def save_profiles(db, key, profiles):
    revision = secrets.token_hex(16)
    sealed = base64.b64encode(crypt(key, "seal", json.dumps(profiles).encode())).decode()
    db.execute("INSERT OR REPLACE INTO profiles VALUES (1, ?, ?)", (revision, sealed))
    return revision


def remove_school(db, key, host):
    for table, key_column in (("snapshots", "source"), ("trackers", "source"), ("portal_snapshots", "id")):
        rows = db.execute(f"SELECT {key_column}, source FROM {table}")
        keys = [(row_id,) for row_id, source in rows if urlsplit(source).hostname == host]
        db.executemany(f"DELETE FROM {table} WHERE {key_column}=?", keys)
    updates = [(row_id,) for row_id, record in db.execute("SELECT id, record FROM updates")
               if urlsplit(json.loads(record)["source"]).hostname == host]
    db.executemany("DELETE FROM updates WHERE id=?", updates)
    for table in ("schools", "school_branding", "new_school_branding", "portal_aliases"):
        db.execute(f"DELETE FROM {table} WHERE host=?", (host,))
    order = read_school_order(db)
    if order is not None and host in order:
        write_school_order(db, [entry for entry in order if entry != host])
    selection = db.execute("SELECT hosts FROM branding_selection WHERE id=1").fetchone()
    if selection:
        db.execute("UPDATE branding_selection SET hosts=? WHERE id=1",
                   (json.dumps([entry for entry in json.loads(selection[0]) if entry != host]),))
    _, profiles = load_profiles(db, key)
    if any(host in profile["hosts"] for profile in profiles):
        for profile in profiles:
            profile["hosts"] = [entry for entry in profile["hosts"] if entry != host]
        save_profiles(db, key, [profile for profile in profiles if profile["hosts"]])


def assign_profile(db, address, school, profile_id):
    url = urlsplit(address)
    source = f"https://{url.netloc}{url.path}"
    db.execute("""INSERT INTO trackers (source, url, school, interval, enabled, profile_id)
        VALUES (?, ?, ?, (SELECT interval FROM tracker_defaults WHERE id=1), 0, ?)
        ON CONFLICT(source) DO UPDATE SET profile_id=excluded.profile_id, checked=NULL,
        result=NULL, checking_until=NULL""", (source, address, school, profile_id))


def brandfetch_configured(directory):
    key_file = directory / "brandfetch-key"
    return bool(os.environ.get("BRANDFETCH_API_KEY")) or (key_file.is_file() and key_file.stat().st_size > 0)


def enable_new_school_branding(db, directory, host):
    if not brandfetch_configured(directory):
        return
    mode = db.execute("SELECT mode FROM branding_settings WHERE id=1").fetchone()
    if mode and mode[0] == "automatic":
        selection = db.execute("SELECT hosts FROM branding_selection WHERE id=1").fetchone()
        if selection:
            hosts = list(dict.fromkeys([*json.loads(selection[0]), host]))
            db.execute("UPDATE branding_selection SET hosts=? WHERE id=1", (json.dumps(hosts),))
    else:
        db.execute("INSERT OR IGNORE INTO new_school_branding VALUES (?)", (host,))


def configure_default_login(db, profiles, hosts, credentials=None):
    profile = next((entry for entry in profiles if entry["id"] == DEFAULT_LOGIN_ID), None)
    if profile is None:
        if credentials is None:
            raise LookupError("Save a default login first.")
        profile = {"id": DEFAULT_LOGIN_ID, "name": "Default login", "hosts": []}
        profiles.append(profile)
    if credentials is not None:
        profile.update(credentials)
        db.execute("""UPDATE trackers SET checked=NULL, result=NULL, checking_until=NULL
            WHERE profile_id=?""", (DEFAULT_LOGIN_ID,))
    profile["hosts"] = list(dict.fromkeys([*profile["hosts"], *hosts]))
    return profile


def set_school_login(db, key, host, address, school, login):
    if not isinstance(login, dict):
        raise ValueError("Choose a portal login option.")
    mode = login.get("mode")
    if mode == "keep":
        return
    _, profiles = load_profiles(db, key)
    email, password = login.get("email"), login.get("password")
    if mode in ("separate", "new-default") and not all(
        isinstance(value, str) and 0 < len(value) <= 1024 for value in (email, password)
    ):
        raise ValueError("Enter the email and password for this login.")
    if mode in ("default", "new-default"):
        credentials = {"email": email, "password": password} if mode == "new-default" else None
        try:
            profile = configure_default_login(db, profiles, [host], credentials)
        except LookupError as error:
            raise ValueError("Create a default login first.") from error
    elif mode == "separate":
        name = f"{host} · School login"
        profile = next((entry for entry in profiles if entry["hosts"] == [host] and entry["name"] == name), None)
        if not profile:
            profile = {"id": secrets.token_hex(16), "name": name, "hosts": [host]}
            profiles.append(profile)
        profile.update(email=email, password=password)
    elif mode == "saved":
        profile = next((entry for entry in profiles if entry["id"] == login.get("id") and host in entry["hosts"]), None)
        if not profile:
            raise ValueError("Choose a saved login assigned to this school.")
    else:
        raise ValueError("Choose a portal login option.")
    if mode != "saved":
        save_profiles(db, key, profiles)
    assign_profile(db, status_address(address), school, profile["id"])


def monitor_portals(directory, key, stop, monitor):
    path = directory / "coll-track.sqlite3"
    while not stop.is_set():
        try:
            now = int(time.time() * 1000)
            with closing(sqlite3.connect(path)) as db, db:
                db.row_factory = sqlite3.Row
                tracker = db.execute("""UPDATE trackers SET checking_until=?
                    WHERE source=(SELECT source FROM trackers
                        WHERE COALESCE(checking_until, 0) <= ?
                        AND (requested IS NOT NULL OR (enabled=1 AND COALESCE(checked, 0) + interval * 60000 <= ?))
                        ORDER BY requested IS NULL, requested, COALESCE(checked, 0), source LIMIT 1)
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
                claimed = db.execute("SELECT 1 FROM trackers WHERE source=? AND checking_until=?",
                                     (tracker["source"], tracker["checking_until"])).fetchone()
                if claimed:
                    if status == "checked" and result is not None:
                        status = save_scan(db, result, tracker)
                    else:
                        db.execute("UPDATE trackers SET pending_page=NULL WHERE source=?", (tracker["source"],))
                    db.execute("UPDATE trackers SET checked=?, result=?, checking_until=NULL, requested=NULL WHERE source=?",
                               (int(time.time() * 1000), status, tracker["source"]))
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            stop.wait(2)


def handler_for(directory, vault_key, port, monitoring=False, dashboard=True):
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

        def cli_api(self):
            return (self.valid_source() and "Origin" not in self.headers
                    and self.headers.get("X-Coll-Track") == "cli")

        def do_GET(self):
            if not self.valid_source():
                return self.reply(403)
            if self.path == "/capabilities":
                return self.reply(200, {"dashboard": dashboard})
            if self.path in assets:
                if not dashboard:
                    return self.reply(404, {"error": "The dashboard is disabled. Other clients can still use this service."})
                filename, content_type = assets[self.path]
                data = (Path(__file__).parent / filename).read_bytes()
                return self.send_bytes(200, data, content_type)
            if self.path == "/cli/portals" and not self.cli_api():
                return self.reply(403)
            if self.path == "/profiles" and not self.extension_api():
                return self.reply(403)
            with closing(sqlite3.connect(directory / "coll-track.sqlite3")) as db:
                if self.path == "/cli/portals":
                    return self.reply(200, shared_schools(db))
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
                    configured = brandfetch_configured(directory)
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
                    auto_new = db.execute("SELECT 1 FROM new_school_branding WHERE host=?", (host,)).fetchone()
                    automatic = auto_new or (selected and mode and mode[0] == "automatic")
                    brand = lookup(directory, domain) if domain and automatic else {}
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
                    for tracker in trackers:
                        tracker.pop("pending_page", None)
                    return self.reply(200, trackers)
            return self.reply(404)

        def do_PUT(self):
            dashboard_write = dashboard and self.path in ("/schools/login", "/schools/remove", "/updates/read", "/trackers/settings", "/trackers/defaults", "/trackers/register", "/trackers/check", "/branding/settings", "/branding/school", "/schools", "/schools/order") and (
                self.valid_source() and self.headers.get("Origin") == f"http://127.0.0.1:{port}"
                and self.headers.get("Content-Type") == "application/json"
            )
            cli_write = self.cli_api() and (self.path.startswith("/cli/") or self.path in
                                             ("/schools", "/schools/remove", "/snapshots", "/trackers/check"))
            if not (dashboard_write or self.extension_api() or cli_write):
                return self.reply(403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= LIMIT:
                    return self.reply(413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    return self.reply(400)
                with closing(sqlite3.connect(directory / "coll-track.sqlite3")) as db, db:
                    if self.path == "/schools/remove":
                        if not (dashboard_write or self.cli_api()):
                            return self.reply(403)
                        host = data.get("host")
                        db.execute("BEGIN IMMEDIATE")
                        if not isinstance(host, str) or not known_school(db, host):
                            return self.reply(404, {"error": "This school is no longer saved."})
                        remove_school(db, vault_key, host)
                    elif self.path == "/schools/order":
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
                        if expected is not None:
                            old = next(record for record in shared_schools(db) if record["host"] == host)
                            previous = urlsplit(old["portal"])
                            new_address = status_address(address)
                            new = urlsplit(new_address)
                            old_source = f"https://{previous.netloc}{previous.path}"
                            new_source = f"https://{new.netloc}{new.path}"
                            if old_source != new_source and db.execute(
                                "SELECT 1 FROM trackers WHERE source=?", (new_source,)
                            ).fetchone():
                                db.execute("UPDATE trackers SET enabled=0, requested=NULL, checking_until=NULL WHERE source=?", (old_source,))
                            else:
                                db.execute("""UPDATE trackers SET source=?, url=?, school=?,
                                    checked=NULL, result=NULL, checking_until=NULL WHERE source=?""",
                                    (new_source, new_address, name.strip(), old_source))
                        db.execute("INSERT OR REPLACE INTO schools VALUES (?, ?, ?)", (host, name.strip(), address))
                        alias = data.get("alias")
                        if alias is not None:
                            if not self.cli_api() or not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
                                db.rollback()
                                return self.reply(400, {"error": "Use letters, numbers, dashes, or underscores for the portal ID."})
                            if db.execute("SELECT 1 FROM portal_aliases WHERE alias=? COLLATE NOCASE", (alias,)).fetchone():
                                db.rollback()
                                return self.reply(409, {"error": "That portal ID is already in use."})
                            db.execute("INSERT OR REPLACE INTO portal_aliases VALUES (?, ?)", (host, alias))
                        if expected is None:
                            enable_new_school_branding(db, directory, host)
                        if "login" in data:
                            try:
                                set_school_login(db, vault_key, host, address, name.strip(), data["login"])
                            except ValueError as error:
                                db.rollback()
                                return self.reply(400, {"error": str(error)})
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
                        db.execute("DELETE FROM new_school_branding")
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
                    elif self.path == "/cli/mark-read":
                        if not self.cli_api():
                            return self.reply(403)
                        db.execute("BEGIN IMMEDIATE")
                        ids = [(id_,) for id_, record in db.execute("SELECT id, record FROM updates WHERE read=0")
                               if urlsplit(json.loads(record)["source"]).hostname == data.get("host")]
                        db.executemany("UPDATE updates SET read=1 WHERE id=?", ids)
                        db.commit()
                        return self.reply(200, {"count": len(ids)})
                    elif self.path == "/cli/launch-profile":
                        if not self.cli_api():
                            return self.reply(403)
                        url = urlsplit(status_address(data["url"]))
                        if not known_school(db, url.hostname):
                            return self.reply(404)
                        source = f"https://{url.netloc}{url.path}"
                        row = db.execute("SELECT profile_id FROM trackers WHERE source=?", (source,)).fetchone()
                        selected = row[0] if row else None
                        _, profiles = load_profiles(db, vault_key)
                        matches = [profile for profile in profiles if url.hostname in profile["hosts"]
                                   and (not selected or profile["id"] == selected)]
                        if len(matches) > 1:
                            return self.reply(409, {"error": "Choose this school’s saved login in dashboard settings."})
                        if not matches:
                            return self.reply(404, {"error": "Save a login for this school first."})
                        return self.reply(200, {field: matches[0][field] for field in ("email", "password")})
                    elif self.path == "/cli/default-login":
                        if not self.cli_api():
                            return self.reply(403)
                        hosts = data.get("hosts")
                        if not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) for host in hosts):
                            return self.reply(400, {"error": "Select at least one saved school."})
                        email, password = data.get("email"), data.get("password")
                        editing = "email" in data or "password" in data
                        if editing and not all(isinstance(value, str) and 0 < len(value) <= 1024 for value in (email, password)):
                            return self.reply(400, {"error": "Enter the email and password for the default login."})
                        db.execute("BEGIN IMMEDIATE")
                        schools = {record["host"]: record for record in shared_schools(db)}
                        if any(host not in schools for host in hosts):
                            return self.reply(400, {"error": "Select saved schools before assigning a login."})
                        _, profiles = load_profiles(db, vault_key)
                        credentials = {"email": email, "password": password} if editing else None
                        try:
                            configure_default_login(db, profiles, hosts, credentials)
                        except LookupError as error:
                            return self.reply(404, {"error": str(error)})
                        save_profiles(db, vault_key, profiles)
                        for host in dict.fromkeys(hosts):
                            school = schools[host]
                            assign_profile(db, status_address(school["portal"]), school["school"], DEFAULT_LOGIN_ID)
                        db.commit()
                        return self.reply(200, {})
                    elif self.path in ("/cli/login", "/schools/login"):
                        allowed = self.cli_api() if self.path == "/cli/login" else (
                            dashboard_write or self.extension_api()
                        )
                        if not allowed:
                            return self.reply(403)
                        address = status_address(data["url"])
                        host = data.get("host")
                        db.execute("BEGIN IMMEDIATE")
                        if host != urlsplit(address).hostname or not known_school(db, host):
                            return self.reply(400, {"error": "Choose a saved school and enter its login."})
                        school = next(record for record in shared_schools(db) if record["host"] == host)
                        login = data.get("login", {"mode": "separate", "email": data.get("email"),
                                                   "password": data.get("password")})
                        try:
                            set_school_login(db, vault_key, host, address, school["school"], login)
                        except ValueError as error:
                            db.rollback()
                            return self.reply(400, {"error": str(error)})
                        db.commit()
                        return self.reply(200, {})
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
                        revision = save_profiles(db, vault_key, profiles)
                        db.commit()
                        return self.reply(200, {"revision": revision})
                    elif self.path == "/snapshots":
                        source, stamp = data.get("source"), data.get("importedAt")
                        if not isinstance(source, str) or not re.fullmatch(r"https://[^?#\s]+", source) or not isinstance(
                            stamp, str
                        ) or not isinstance(data.get("checklists"), list):
                            return self.reply(400)
                        if not checklist_readable(data):
                            return self.reply(422, {"error": "This checklist is incomplete. Any previously saved checklist was kept."})
                        db.execute("BEGIN IMMEDIATE")
                        previous = db.execute("SELECT imported_at FROM snapshots WHERE source=?", (source,)).fetchone()
                        if previous and previous[0] >= stamp:
                            return self.reply(409, {"error": "That snapshot is already saved or is older than the latest one."})
                        host = urlsplit(source).hostname
                        is_new = host and not known_school(db, host)
                        save_snapshot(db, data)
                        if is_new:
                            enable_new_school_branding(db, directory, host)
                    elif self.path == "/updates/read":
                        id_ = data.get("id")
                        read = data.get("read")
                        if type(id_) is not int or type(read) is not bool:
                            return self.reply(400)
                        db.execute("UPDATE updates SET read=? WHERE id=?", (int(read), id_))
                    elif self.path in ("/trackers/check", "/cli/tracking"):
                        settings = self.path == "/cli/tracking"
                        if settings and not self.cli_api():
                            return self.reply(403)
                        if not monitoring and not settings:
                            return self.reply(503, {"error": "Start the Coll-Track background checker first."})
                        address = status_address(data["url"])
                        url = urlsplit(address)
                        school = data["school"]
                        db.execute("BEGIN IMMEDIATE")
                        if not isinstance(school, str) or not known_school(db, url.hostname):
                            return self.reply(400, {"error": "Add this school before checking it."})
                        source = f"https://{url.netloc}{url.path}"
                        if settings:
                            interval, enabled = data.get("interval"), data.get("enabled")
                            if enabled is False and interval == 0:
                                db.execute("UPDATE trackers SET enabled=0 WHERE source=?", (source,))
                                db.commit()
                                return self.reply(200, {})
                            if not check_interval(interval) or type(enabled) is not bool:
                                return self.reply(400, {"error": "Enter an interval between 1 and 10080 minutes."})
                            db.execute("""INSERT INTO trackers (source, url, school, interval, enabled)
                                VALUES (?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET
                                interval=excluded.interval, enabled=excluded.enabled, url=excluded.url, school=excluded.school""",
                                (source, address, school, interval, int(enabled)))
                            db.commit()
                            return self.reply(200, {})
                        enable = data.get("enable", False)
                        if type(enable) is not bool:
                            return self.reply(400)
                        now = int(time.time() * 1000)
                        db.execute("""INSERT INTO trackers (source, url, school, interval, enabled, requested)
                            VALUES (?, ?, ?, (SELECT interval FROM tracker_defaults WHERE id=1), ?, ?)
                            ON CONFLICT(source) DO UPDATE SET requested=COALESCE(trackers.requested, excluded.requested),
                            url=excluded.url, school=excluded.school, enabled=MAX(trackers.enabled, excluded.enabled)""",
                            (source, address, school, int(enable), now))
                        requested = db.execute("SELECT requested FROM trackers WHERE source=?", (source,)).fetchone()[0]
                        db.commit()
                        return self.reply(202, {"source": source, "requested": requested})
                    elif self.path == "/trackers/register":
                        url = urlsplit(data.get("url", ""))
                        source = data.get("source")
                        school = data.get("school")
                        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.port
                            or url.path.startswith("/account/") or url.fragment
                            or source != f"https://{url.netloc}{url.path}" or not isinstance(school, str)
                            or not known_school(db, url.hostname)):
                            return self.reply(400)
                        interval = data.get("interval")
                        enabled = data.get("enabled", True)
                        if (interval is not None and not check_interval(interval)) or type(enabled) is not bool:
                            return self.reply(400, {"error": "Use a check interval from 1 to 10,080 minutes."})
                        profile_id = data.get("profileId")
                        if profile_id is not None:
                            _, profiles = load_profiles(db, vault_key)
                            if not any(profile["id"] == profile_id and url.hostname in profile["hosts"] for profile in profiles):
                                return self.reply(400, {"error": "Choose a saved login assigned to this portal."})
                        db.execute("""INSERT INTO trackers (source, url, school, interval, enabled, profile_id)
                            VALUES (?, ?, ?, COALESCE(?, (SELECT interval FROM tracker_defaults WHERE id=1)), ?, ?)
                            ON CONFLICT(source) DO UPDATE SET url=excluded.url, school=excluded.school,
                            interval=COALESCE(?, trackers.interval), enabled=excluded.enabled,
                            profile_id=CASE WHEN ? THEN excluded.profile_id ELSE trackers.profile_id END,
                            checking_until=CASE WHEN ? AND trackers.profile_id IS NOT excluded.profile_id
                                THEN NULL ELSE trackers.checking_until END,
                            checked=CASE WHEN ? AND trackers.profile_id IS NOT excluded.profile_id
                                THEN NULL ELSE trackers.checked END""",
                            (source, url.geturl(), school, interval, int(enabled), profile_id, interval,
                             "profileId" in data, "profileId" in data, "profileId" in data))
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
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args()
    key = initialize(args.data_dir)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.data_dir, key, args.port, args.monitor, not args.no_dashboard)) as server:
        stop = Event()
        if args.monitor:
            from portal_monitor import PortalMonitor
            Thread(target=monitor_portals, args=(args.data_dir, key, stop, PortalMonitor()), daemon=True).start()
        try:
            server.serve_forever()
        finally:
            stop.set()
