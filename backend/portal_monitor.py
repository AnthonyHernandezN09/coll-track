from datetime import datetime, timezone
from hashlib import sha256
from json import dumps
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright


class PortalMonitor:
    def __init__(self):
        directory = Path(__file__).parent
        scripts = directory if (directory / "checklist-reader.js").is_file() else directory.parent
        self.reader = (scripts / "checklist-reader.js").read_text()
        self.capture = (directory / "portal-reader.js").read_text()
        self.login = (scripts / "slate-login.js").read_text()
        self.playwright = None
        self.browser = None
        self.sessions = {}
        self.rejected = {}

    def session(self, host, profile):
        identity = sha256(dumps([profile[key] for key in ("id", "email", "password")]).encode()).digest() if profile else None
        if not self.browser or not self.browser.is_connected():
            if self.playwright:
                self.playwright.stop()
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True, chromium_sandbox=True, timeout=10000)
            self.sessions.clear()
        previous = self.sessions.get(host)
        if previous and previous[0] == identity:
            return previous[1], identity
        if previous:
            previous[1].close()
        context = self.browser.new_context(accept_downloads=False)
        self.sessions[host] = (identity, context)
        return context, identity

    def scan(self, tracker, profiles):
        target = urlsplit(tracker["url"])
        matches = [profile for profile in profiles if target.hostname in profile["hosts"]]
        profile_id = tracker["profile_id"]
        profile = next((entry for entry in matches if entry["id"] == profile_id), None)
        if not profile_id and len(matches) == 1:
            profile = matches[0]
        context, identity = self.session(target.hostname, profile)
        page = context.new_page()
        page.set_default_timeout(6000)
        try:
            response = page.goto(tracker["url"], wait_until="domcontentloaded", timeout=12000)
            if not response or response.status >= 400:
                return "error", None
            current = urlsplit(page.url)
            if current.netloc != target.netloc or current.scheme != "https":
                return "verification", None
            if current.path == "/account/login":
                if not profile:
                    return ("choose_login" if len(matches) > 1 and not profile_id else "missing_login"), None
                rejected = self.rejected.get(target.hostname)
                if rejected and rejected[0] == identity:
                    return rejected[1], None
                result = page.evaluate(f"args => ({self.login})(...args)",
                                       [page.url, profile["email"], profile["password"]])
                if result != "submitted":
                    return "verification", None
                try:
                    page.wait_for_url(lambda url: urlsplit(url).path != "/account/login", timeout=7500,
                                      wait_until="domcontentloaded")
                except TimeoutError:
                    self.rejected[target.hostname] = (identity, "login_failed")
                    return "login_failed", None
                current = urlsplit(page.url)
                if current.netloc != target.netloc or current.scheme != "https" or current.path.startswith("/account/"):
                    self.rejected[target.hostname] = (identity, "verification")
                    return "verification", None
                if current.path != target.path:
                    response = page.goto(tracker["url"], wait_until="domcontentloaded", timeout=8500)
                    if not response or response.status >= 400:
                        return "error", None
            expected = (target.scheme, target.netloc, target.path.rstrip("/"))
            page.evaluate(self.reader)
            previous = None
            for _ in range(8):
                current = urlsplit(page.url)
                if (current.scheme, current.netloc, current.path.rstrip("/")) != expected:
                    return "verification" if current.path.startswith("/account/") else "unreadable", None
                result = page.evaluate(self.capture)
                if result and result == previous:
                    result["importedAt"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    return "checked", result
                previous = result
                page.wait_for_timeout(750)
            return "unreadable", None
        except Error:
            return "error", None
        finally:
            if not page.is_closed():
                page.close()
