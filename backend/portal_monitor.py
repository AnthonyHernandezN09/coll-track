import re
from datetime import datetime, timezone
from hashlib import sha256
from json import dumps
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urljoin, urlsplit

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
        pending_content = set()
        failed_content = False

        def is_portal_content(request):
            address = urlsplit(request.url)
            return (request.resource_type in ("document", "xhr", "fetch")
                    and (address.scheme, address.netloc) == (target.scheme, target.netloc))

        def request_started(request):
            if is_portal_content(request):
                pending_content.add(request)

        def request_failed(request):
            nonlocal failed_content
            if request in pending_content and request.failure != "net::ERR_ABORTED":
                failed_content = True
            pending_content.discard(request)

        def response_received(response):
            nonlocal failed_content
            if response.status >= 500 and is_portal_content(response.request):
                failed_content = True

        page.on("request", request_started)
        page.on("requestfinished", lambda request: pending_content.discard(request))
        page.on("requestfailed", request_failed)
        page.on("response", response_received)

        page.set_default_timeout(6000)
        try:
            response = page.goto(tracker["url"], wait_until="domcontentloaded", timeout=12000)
            if not response or response.status >= 400:
                return "error", None
            current = urlsplit(page.url)
            if current.netloc != target.netloc or current.scheme != "https":
                return "verification", None
            if current.path.rstrip("/") == "/apply":
                login_links = page.get_by_role("link", name=re.compile(r"^(log|sign) in$", re.I))
                if login_links.count() == 1:
                    href = login_links.get_attribute("href")
                    login = urlsplit(urljoin(page.url, href or ""))
                    if (login.scheme, login.netloc, login.path.rstrip("/")) == (
                        target.scheme, target.netloc, "/account/login"
                    ):
                        response = page.goto(login.geturl(), wait_until="domcontentloaded", timeout=8500)
                        if not response or response.status >= 400:
                            return "error", None
                        current = urlsplit(page.url)
                        if current.scheme != target.scheme or current.netloc != target.netloc:
                            return "verification", None
            if current.path.rstrip("/") == "/account/login":
                if not profile:
                    return ("choose_login" if len(matches) > 1 and not profile_id else "missing_login"), None
                rejected = self.rejected.get(target.hostname)
                if rejected and rejected[0] == identity and rejected[2] > monotonic():
                    return rejected[1], None
                login_url = page.url
                try:
                    page.wait_for_function("typeof globalThis.FW?.Postback === 'function'", timeout=3000)
                except TimeoutError:
                    return "verification", None
                result = page.evaluate(f"args => ({self.login})(...args)",
                                       [login_url, profile["email"], profile["password"]])
                if result != "submitted":
                    return "verification", None
                try:
                    page.wait_for_url(lambda url: urlsplit(url).path.rstrip("/") != "/account/login", timeout=7500,
                                      wait_until="domcontentloaded")
                except TimeoutError:
                    if page.evaluate(
                        r"() => /invalid email address or password|incorrect password|invalid credentials/i.test(document.body?.innerText ?? '')"
                    ):
                        self.rejected[target.hostname] = (identity, "login_failed", monotonic() + 900)
                        return "login_failed", None
                    return "login_timeout", None
                current = urlsplit(page.url)
                if current.netloc != target.netloc or current.scheme != "https" or current.path.startswith("/account/"):
                    self.rejected[target.hostname] = (identity, "verification", monotonic() + 900)
                    return "verification", None
                if current.path.rstrip("/") != target.path.rstrip("/") or current.query != target.query:
                    response = page.goto(tracker["url"], wait_until="domcontentloaded", timeout=8500)
                    if not response or response.status >= 400:
                        return "error", None
            status_fallback = False
            expected = (target.scheme, target.netloc, target.path.rstrip("/"))
            page.evaluate(self.reader)
            previous = None
            selected_application_tab = False
            for attempt in range(12):
                current = urlsplit(page.url)
                if (current.scheme, current.netloc, current.path.rstrip("/")) != expected:
                    return "verification" if current.path.startswith("/account/") else "unreadable", None
                if failed_content:
                    return "error", None
                if pending_content:
                    previous = None
                    page.wait_for_timeout(750)
                    continue
                result = page.evaluate(self.capture)
                if (attempt >= 2 and result and not result["checklists"]
                        and expected[2] == "/apply" and not status_fallback):
                    status_fallback = True
                    status_url = target._replace(path="/apply/status", query="", fragment="")
                    response = page.goto(status_url.geturl(), wait_until="domcontentloaded", timeout=8500)
                    if not response or response.status >= 400:
                        return "status_page_missing", None
                    landed = urlsplit(page.url)
                    if (landed.scheme, landed.netloc, landed.path.rstrip("/")) != (
                        target.scheme, target.netloc, "/apply/status"
                    ):
                        return "status_page_missing", None
                    expected = (target.scheme, target.netloc, "/apply/status")
                    page.evaluate(self.reader)
                    previous = None
                    continue
                if attempt >= 2 and result and not result["checklists"] and not selected_application_tab and not parse_qs(target.query).get("tab"):
                    application_tab = page.locator('ul.subtabs li > a[data-tab="application"]:visible')
                    if application_tab.count() == 1:
                        application_tab.click(timeout=1500)
                        selected_application_tab = True
                        previous = None
                        page.wait_for_timeout(750)
                        continue
                if result and result["checklists"] and result == previous:
                    if failed_content:
                        return "error", None
                    if pending_content:
                        previous = None
                        page.wait_for_timeout(750)
                        continue
                    result["importedAt"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    return "checked", result
                previous = result
                page.wait_for_timeout(750)
            return ("error" if failed_content or pending_content else "unreadable"), None
        except Error:
            return "error", None
        finally:
            if not page.is_closed():
                page.close()
