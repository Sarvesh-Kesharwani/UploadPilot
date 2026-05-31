"""
YouLearn page automation.

Selector strategy: prefer semantic role/text locators so a CSS class change doesn't
break us. Where the DOM is unknown, we try multiple fallback selectors in order.
Every action is logged so we can diagnose breaks from the log stream in the UI.
"""
from __future__ import annotations
from collections import Counter
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, Locator

log = logging.getLogger(__name__)

BROWSER_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
)

# Heuristic selectors. Tuned on first real run; update here if YouLearn's DOM shifts.
SEL_ADD_CONTENT = [
    'main button:has(span:text-is("Add Content"))',
    'main button:has-text("Add Content")',
    'button:has(span:text-is("Add Content"))',
    'button:has-text("Add Content")',
    '[aria-label*="add content" i]',
]
SEL_UPLOAD_MENUITEM = [
    '[role="dialog"] div.cursor-pointer:has(h3:text-is("Upload"))',
    '[role="dialog"] div.group:has(h3:text-is("Upload"))',
    'div.cursor-pointer:has(h3:text-is("Upload"))',
    'div.group:has(h3:text-is("Upload"))',
    'div:has(> div h3:text-is("Upload"))',
    'h3:text-is("Upload")',
]
# Each video row in the space listing — visible in the screenshot as
# [▷ title ............. N days ago ⋮]
SEL_CONTENT_ROWS = [
    'tbody tr[role="button"]:has(span.truncate)',
    'tbody tr:has(span.truncate)',
    'tbody tr[role="button"]:has(svg.lucide-play)',
    'tbody tr:has(svg.lucide-play)',
]
SEL_VIDEO_ROWS = SEL_CONTENT_ROWS
SEL_ANY_ROW = 'tbody tr'  # for wait-until-list-loaded
# Title span inside a row (used to read current titles & to filter new row).
SEL_ROW_TITLE = 'span.truncate'
SEL_KEBAB = [
    'td:last-child svg[aria-label="optionsMenu.openMenu"]',
    'td:last-child svg.lucide-ellipsis-vertical',
    'svg[aria-label="optionsMenu.openMenu"]',
    'td:last-child button',
    'td.text-right button',
    'button[aria-haspopup="menu"]',
    'button[id^="radix-"]',
    'button:has(svg.lucide-ellipsis)',
    'button:has(svg.lucide-ellipsis-vertical)',
    'button:has(svg[class*="ellipsis"])',
    'button[aria-label*="more" i]',
    'button:has-text("⋮")',
]
# Rename flow (new): open video, click inline header title, type, Enter.
SEL_HEADER_TITLE = [
    'header span.cursor-text.truncate',
    'header span.cursor-text',
    'span.cursor-text.truncate',
    'span.cursor-text',
]
# After clicking, title may swap to input or become contenteditable.
SEL_TITLE_EDIT_ACTIVE = [
    'header input[type="text"]:visible',
    'header textarea:visible',
    'header [contenteditable="true"]',
    'input[type="text"]:focus',
    '[contenteditable="true"]:focus',
]
SEL_EDIT_MENUITEM = [
    '[role="menuitem"]:has-text("Rename")',
    '[role="menuitem"]:has-text("Edit")',
    'button:has-text("Rename")',
    'button:has-text("Edit")',
    'text=/^Rename$/i',
    'text=/^Edit$/i',
]
SEL_SIGN_IN_EMAIL_METHOD = [
    'button:has-text("Sign in with email")',
    'button:has-text("Continue with email")',
    'a:has-text("Sign in with email")',
    'a:has-text("Continue with email")',
    'button:has-text("Email")',
]
SEL_EMAIL_INPUT = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
]
SEL_PASSWORD_INPUT = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[placeholder*="password" i]',
]
SEL_CONTINUE_BUTTON = [
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Submit")',
]
SEL_LOGIN_SUBMIT = [
    'form button[type="submit"]',
    'button[type="submit"]',
    'role=button[name=/^sign in$/i]',
    'form button:has-text("Sign In")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
]
SEL_LOGGED_IN_HINTS = [
    '[href*="/space"]',
    'button[aria-label*="profile" i]',
    '[aria-label*="account" i]',
    'text=/spaces/i',
    'text=/dashboard/i',
]
SEL_LOGGED_OUT_HINTS = [
    'text=/sign in/i',
    'text=/log in/i',
    'text=/continue with google/i',
    'text=/sign in with google/i',
]


def _first(page_or_locator, selectors: list[str]) -> Locator | None:
    """Return the first visible match across selectors."""
    for sel in selectors:
        try:
            loc = page_or_locator.locator(sel)
            loc.first.wait_for(state="attached", timeout=1500)
            count = min(loc.count(), 30)
        except Exception:
            continue
        for i in range(count):
            candidate = loc.nth(i)
            try:
                candidate.wait_for(state="visible", timeout=250)
                return candidate
            except Exception:
                continue
    return None


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def _title_matches_target(display_title: str, target_title: str) -> bool:
    return _normalize_title(display_title) == _normalize_title(target_title)


def _visible_title_can_be_target(display_title: str, target_title: str) -> bool:
    display = _normalize_title(display_title)
    target = _normalize_title(target_title)
    if display == target:
        return True
    target_path = Path(target_title)
    if target_path.suffix:
        return display == _normalize_title(target_path.stem)
    return False


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{query}".casefold()


def _target_url_matches(actual_url: str, target_url: str) -> bool:
    actual = urlsplit(actual_url)
    target = urlsplit(target_url)
    actual_path = actual.path.rstrip("/").casefold()
    target_path = target.path.rstrip("/").casefold()
    if actual.netloc.casefold() != target.netloc.casefold() or actual_path != target_path:
        return False
    target_folder = parse_qs(target.query).get("folderId", [""])[0]
    if not target_folder:
        return True
    actual_folder = parse_qs(actual.query).get("folderId", [""])[0]
    return actual_folder == target_folder


def _is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in BROWSER_CLOSED_MARKERS)


class YouLearn:
    def __init__(self, ctx: BrowserContext, dashboard_url: str, login_url: str,
                 poll_interval_s: float, poll_timeout_s: float):
        self.ctx = ctx
        self.dashboard_url = dashboard_url
        self.login_url = login_url
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.last_content_url = ""

    # ---- session --------------------------------------------------------

    def page(self) -> Page:
        if self.ctx.pages:
            return self.ctx.pages[0]
        return self.ctx.new_page()

    def ensure_logged_in(self, email: str, password: str) -> None:
        """Navigate to dashboard and sign in with email/password if needed."""
        p = self.page()
        p.goto(self.dashboard_url, wait_until="domcontentloaded")
        p.wait_for_timeout(1500)
        if self._is_logged_in(p):
            return
        if not (email.strip() and password):
            raise RuntimeError(
                "YouLearn credentials are missing. Add YOULERN_EMAIL and "
                "YOULERN_PASSWORD to .env, then retry."
            )
        self._login_with_password(p, email.strip(), password)
        p.goto(self.dashboard_url, wait_until="domcontentloaded")
        p.wait_for_timeout(1500)
        if not self._is_logged_in(p):
            raise RuntimeError("Email/password login did not complete successfully")

    def _is_logged_in(self, p: Page) -> bool:
        url = p.url.lower()
        if "login" in url or "signin" in url:
            return False
        for sel in SEL_LOGGED_IN_HINTS:
            try:
                if p.locator(sel).first.is_visible():
                    return True
            except Exception:
                continue
        for sel in SEL_LOGGED_OUT_HINTS:
            try:
                if p.locator(sel).first.is_visible():
                    return False
            except Exception:
                continue
        return "login" not in url and "signin" not in url

    def _login_with_password(self, p: Page, email: str, password: str) -> None:
        log.info("attempting YouLearn email/password login")
        p.goto(self.login_url, wait_until="domcontentloaded")
        p.wait_for_timeout(1200)

        email_method = _first(p, SEL_SIGN_IN_EMAIL_METHOD)
        if email_method:
            email_method.click()
            p.wait_for_timeout(800)

        email_input = _first(p, SEL_EMAIL_INPUT)
        if not email_input:
            raise RuntimeError("Email input not found on YouLearn login page")
        email_input.click()
        email_input.fill(email)
        p.wait_for_timeout(200)

        password_input = _first(p, SEL_PASSWORD_INPUT)
        if not password_input:
            continue_btn = _first(p, SEL_CONTINUE_BUTTON) or _first(p, SEL_LOGIN_SUBMIT)
            if continue_btn:
                continue_btn.click()
                p.wait_for_timeout(1000)
            password_input = _first(p, SEL_PASSWORD_INPUT)
        if not password_input:
            raise RuntimeError("Password input not found on YouLearn login page")

        password_input.click()
        password_input.fill(password)
        p.wait_for_timeout(200)

        submit = _first(p, SEL_LOGIN_SUBMIT) or _first(p, SEL_CONTINUE_BUTTON)
        if submit:
            try:
                submit.click(timeout=3000)
            except Exception:
                try:
                    submit.click(force=True)
                except Exception:
                    submit.evaluate("(el) => el.click()")
        else:
            password_input.press("Enter")

        p.wait_for_timeout(1200)
        if not self._is_logged_in(p) and _first(p, SEL_PASSWORD_INPUT):
            try:
                p.locator("form").first.evaluate("(form) => form.requestSubmit()")
            except Exception:
                pass
            p.wait_for_timeout(800)

        if not self._is_logged_in(p) and _first(p, SEL_PASSWORD_INPUT):
            password_input.press("Enter")

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            p.wait_for_timeout(500)
            if self._is_logged_in(p):
                log.info("YouLearn email/password login succeeded")
                return
        raise RuntimeError("Timed out waiting for YouLearn to finish signing in")

    # ---- space listing --------------------------------------------------

    def open_space(self, space_url: str, *, force_reload: bool = False) -> None:
        p = self.page()
        if force_reload or not _target_url_matches(p.url, space_url):
            log.info("navigating to space %s", space_url)
            p.goto(space_url, wait_until="domcontentloaded")
            p.wait_for_timeout(1200)
        if not _target_url_matches(p.url, space_url):
            raise RuntimeError(f"Failed to open target space: expected {space_url}, got {p.url}")
        log.info("confirmed target space %s", p.url)

    def list_video_titles(self) -> list[str]:
        """Return titles currently visible in the space content list."""
        p = self.page()
        # wait briefly for any row to render so first read isn't empty
        try:
            p.wait_for_selector(SEL_ANY_ROW, timeout=15000, state="attached")
        except PWTimeout:
            pass
        for sel in SEL_VIDEO_ROWS:
            rows = p.locator(sel)
            try:
                count = rows.count()
            except Exception:
                count = 0
            if count == 0:
                continue
            titles = []
            for i in range(count):
                span = rows.nth(i).locator(SEL_ROW_TITLE).first
                try:
                    t = span.inner_text(timeout=1000).strip()
                except Exception:
                    t = rows.nth(i).inner_text().strip().splitlines()[0].strip()
                if t:
                    titles.append(t)
            if titles:
                return titles
        return []

    # ---- upload ---------------------------------------------------------

    def upload_one(self, space_url: str, file_path: Path) -> str:
        """
        Upload a single file. Returns the *displayed* title on YouLearn once
        the new row appears (this is the auto-generated title we must rename).
        """
        self.open_space(space_url)
        p = self.page()
        before = self.list_video_titles()
        log.info("titles before upload: %d", len(before))

        upload_item = self._open_upload_menu(p)

        # The upload item triggers a native file picker. Intercept it.
        with p.expect_file_chooser() as fc_info:
            upload_item.click()
        chooser = fc_info.value
        chooser.set_files(str(file_path))
        log.info("file chooser accepted: %s", file_path.name)

        # Poll the list until a new title appears.
        new_title = self._wait_for_new_row(before)
        self.last_content_url = p.url if "/content/" in p.url else ""
        log.info("new row appeared as: %r", new_title)
        return new_title

    def wait_for_batch_to_settle(
        self,
        space_url: str,
        initial_titles: list[str],
        target_titles: list[str],
        timeout_s: float,
        poll_s: float,
    ) -> list[str]:
        """
        Wait once for newest uploaded batch to stop changing. YouLearn lists
        newest first, so top N rows map back to queue order after reversing.
        """
        if not initial_titles:
            return []
        deadline = time.monotonic() + timeout_s
        initial_keys = [_normalize_title(t) for t in initial_titles]
        target_keys = [_normalize_title(t) for t in target_titles]
        last_queue_titles = initial_titles

        while time.monotonic() < deadline:
            self.open_space(space_url, force_reload=True)
            top_titles = self.list_video_titles()[:len(initial_titles)]
            if len(top_titles) < len(initial_titles):
                time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
                continue
            queue_titles = list(reversed(top_titles))
            last_queue_titles = queue_titles
            log.info("batch titles while settling: %r", queue_titles)
            settled = [
                _normalize_title(title) not in {initial_keys[i], target_keys[i]}
                for i, title in enumerate(queue_titles)
            ]
            if all(settled):
                time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
                self.open_space(space_url, force_reload=True)
                fresh = list(reversed(self.list_video_titles()[:len(initial_titles)]))
                if [_normalize_title(t) for t in fresh] == [_normalize_title(t) for t in queue_titles]:
                    log.info("batch titles settled: %r", queue_titles)
                    return queue_titles
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))

        log.info("batch settle timeout; using latest titles: %r", last_queue_titles)
        return last_queue_titles

    def _open_upload_menu(self, p: Page) -> Locator:
        last_error = ""
        for attempt in range(3):
            add = _first(p, SEL_ADD_CONTENT)
            if not add:
                last_error = "Add content button not found"
            else:
                try:
                    add.click()
                    p.wait_for_timeout(600 + attempt * 300)
                    upload_item = _first(p, SEL_UPLOAD_MENUITEM)
                    if upload_item:
                        return upload_item
                    last_error = '"Upload" menu item not found'
                except Exception as e:
                    last_error = str(e)
            try:
                p.keyboard.press("Escape")
            except Exception:
                pass
            p.wait_for_timeout(500)
        raise RuntimeError(last_error or '"Upload" menu item not found')

    def _wait_for_new_row(self, before: list[str]) -> str:
        before_counts = Counter(_normalize_title(t) for t in before)
        deadline = time.monotonic() + self.poll_timeout_s
        last_seen: list[str] = []
        while time.monotonic() < deadline:
            content_title = self._current_content_title()
            if content_title:
                log.info("upload landed on content page as: %r", content_title)
                return content_title
            # Refresh the listing without reloading — YouLearn updates it live.
            try:
                current = self.list_video_titles()
            except Exception as e:
                if _is_browser_closed_error(e):
                    raise RuntimeError("Browser was closed while waiting for upload to finish") from e
                log.warning("list read failed: %s", e)
                current = last_seen
            seen_counts: Counter[str] = Counter()
            for title in current:
                key = _normalize_title(title)
                seen_counts[key] += 1
                if seen_counts[key] > before_counts[key]:
                    return title
                # Newest-on-top → first in list is the just-uploaded one.
            last_seen = current
            time.sleep(self.poll_interval_s)
        raise TimeoutError("Uploaded video did not appear in the space list")

    def _current_content_title(self) -> str | None:
        p = self.page()
        if "/content/" not in p.url:
            return None
        for sel in SEL_HEADER_TITLE:
            try:
                loc = p.locator(sel)
                count = min(loc.count(), 30)
            except Exception:
                continue
            visible_texts: list[str] = []
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    candidate.wait_for(state="visible", timeout=150)
                    text = candidate.inner_text(timeout=500).strip()
                except Exception:
                    continue
                if text:
                    visible_texts.append(text)
            if visible_texts:
                return visible_texts[-1]
        return None

    def wait_for_latest_upload_to_settle(
        self,
        space_url: str,
        initial_title: str,
        target_title: str,
        timeout_s: float,
        poll_s: float,
    ) -> str:
        """
        YouLearn can replace the file-name row with an AI-generated title after
        processing. The newest row is the just-uploaded item in this serial
        uploader, so wait for that title to either change and stabilize or stay
        unchanged until the settle timeout expires.
        """
        deadline = time.monotonic() + timeout_s
        initial_key = _normalize_title(initial_title)
        target_key = _normalize_title(target_title)
        last_seen = initial_title

        while time.monotonic() < deadline:
            self.open_space(space_url, force_reload=True)
            titles = self.list_video_titles()
            if titles:
                latest = titles[0]
                last_seen = latest
                latest_key = _normalize_title(latest)
                log.info("latest uploaded row while settling: %r", latest)
                if latest_key not in {initial_key, target_key}:
                    time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
                    self.open_space(space_url, force_reload=True)
                    fresh_titles = self.list_video_titles()
                    if fresh_titles and _normalize_title(fresh_titles[0]) == latest_key:
                        log.info("uploaded row title settled as generated title: %r", latest)
                        return latest
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))

        log.info("uploaded row title stayed as %r through settle timeout", last_seen)
        return last_seen

    # ---- rename ---------------------------------------------------------

    def rename_top_video(self, current_display_title: str, new_title: str) -> None:
        """
        Kebab → Rename flow. Row span becomes editable; type new name + Enter.
        """
        if self._ensure_header_title(new_title):
            return
        if self._rename_visible_row_menu(current_display_title, new_title):
            return
        if self._open_video_row(current_display_title) and self._ensure_header_title(new_title):
            return
        raise RuntimeError(f"Unable to rename title {current_display_title!r} to {new_title!r}")

    def rename_content_url(self, content_url: str, new_title: str) -> bool:
        if not content_url:
            return False
        p = self.page()
        p.goto(content_url, wait_until="domcontentloaded")
        p.wait_for_timeout(1200)
        return self._ensure_header_title(new_title)

    def current_title_for_content_url(self, content_url: str) -> str:
        if not content_url:
            return ""
        p = self.page()
        p.goto(content_url, wait_until="domcontentloaded")
        p.wait_for_timeout(1200)
        return self._current_content_title() or ""

    def rename_video_at_list_index(self, space_url: str, row_index: int, new_title: str) -> str:
        """
        Rename by visible row position. Smart mode uses this after batch upload
        because generated titles can collide or replace original filenames.
        """
        self.open_space(space_url, force_reload=True)
        row = self._row_at_index(row_index)
        if row is None:
            raise RuntimeError(f"Unable to find uploaded row at index {row_index}")
        current_title = self._row_title(row)
        if self._rename_row(row, new_title):
            return current_title
        if self._open_video_row(current_title) and self._ensure_header_title(new_title):
            return current_title
        raise RuntimeError(f"Unable to rename row {row_index} from {current_title!r} to {new_title!r}")

    def _row_at_index(self, row_index: int) -> Locator | None:
        p = self.page()
        for sel in SEL_VIDEO_ROWS:
            rows = p.locator(sel)
            try:
                if rows.count() > row_index:
                    row = rows.nth(row_index)
                    row.wait_for(state="visible", timeout=3000)
                    return row
            except Exception:
                continue
        return None

    def _row_title(self, row: Locator) -> str:
        try:
            return row.locator(SEL_ROW_TITLE).first.inner_text(timeout=1000).strip()
        except Exception:
            return row.inner_text(timeout=1000).strip().splitlines()[0].strip()

    def _rename_row(self, row: Locator, new_title: str) -> bool:
        p = self.page()
        try:
            row.hover()
        except Exception:
            pass
        kebab = None
        for sel in SEL_KEBAB:
            k = row.locator(sel).first
            try:
                k.wait_for(state="visible", timeout=3000)
                kebab = k
                break
            except PWTimeout:
                continue
        if not kebab:
            return False
        kebab.click()
        p.wait_for_timeout(400)

        rename_item = _first(p, SEL_EDIT_MENUITEM)
        if not rename_item:
            return False
        rename_item.click()
        p.wait_for_timeout(700)

        active = _first(p, [
            'tbody tr input[type="text"]:visible',
            'tbody tr [contenteditable="true"]',
            'input[type="text"]:focus',
            '[contenteditable="true"]:focus',
        ])
        if active:
            try:
                active.press("Control+A")
                active.press("Delete")
                active.type(new_title, delay=20)
            except Exception:
                p.keyboard.press("Control+A")
                p.keyboard.press("Delete")
                p.keyboard.type(new_title, delay=20)
        else:
            p.keyboard.press("Control+A")
            p.keyboard.press("Delete")
            p.keyboard.type(new_title, delay=20)
        p.wait_for_timeout(400)
        try:
            p.mouse.click(80, 400)
        except Exception:
            try:
                p.locator("main").first.click(position={"x": 20, "y": 20})
            except Exception:
                pass
        p.wait_for_timeout(800)
        log.info("renamed row to: %r", new_title)
        return True

    def _rename_visible_row_menu(self, current_display_title: str, new_title: str) -> bool:
        p = self.page()
        # 1. Find and click the row whose span text == current_display_title
        row = None
        for sel in SEL_VIDEO_ROWS:
            candidate = p.locator(sel).filter(has_text=current_display_title).first
            try:
                candidate.wait_for(state="visible", timeout=2000)
                row = candidate
                break
            except PWTimeout:
                continue
        if row is None:
            return False

        # 1. Hover row → click kebab ⋮
        try:
            row.hover()
        except Exception:
            pass
        kebab = None
        for sel in SEL_KEBAB:
            k = row.locator(sel).first
            try:
                k.wait_for(state="visible", timeout=3000)
                kebab = k
                break
            except PWTimeout:
                continue
        if not kebab:
            return False
        kebab.click()
        p.wait_for_timeout(400)

        # 2. Radix menu → click Rename
        rename_item = _first(p, SEL_EDIT_MENUITEM)
        if not rename_item:
            return False
        rename_item.click()
        p.wait_for_timeout(700)

        # 3. Row span now editable. Type new title on focused element.
        active = _first(p, [
            'tbody tr input[type="text"]:visible',
            'tbody tr [contenteditable="true"]',
            'input[type="text"]:focus',
            '[contenteditable="true"]:focus',
        ])
        if active:
            try:
                active.press("Control+A")
                active.press("Delete")
                active.type(new_title, delay=20)
            except Exception:
                p.keyboard.press("Control+A")
                p.keyboard.press("Delete")
                p.keyboard.type(new_title, delay=20)
        else:
            # no detectable editable — fallback to keyboard on whatever has focus
            p.keyboard.press("Control+A")
            p.keyboard.press("Delete")
            p.keyboard.type(new_title, delay=20)
        p.wait_for_timeout(400)

        # 4. Commit by clicking empty UI area (NOT Enter — Enter inserts newline).
        #    Space page has safe empty zone around top-left/margins.
        try:
            p.mouse.click(80, 400)
        except Exception:
            try:
                p.locator("main").first.click(position={"x": 20, "y": 20})
            except Exception:
                pass
        p.wait_for_timeout(800)
        log.info("renamed row to: %r", new_title)
        return True

    def _ensure_current_or_opened_video_title(self, current_display_title: str, new_title: str) -> bool:
        if self._ensure_header_title(new_title):
            return True
        if self._open_video_row(current_display_title):
            return self._ensure_header_title(new_title)
        return False

    def _open_video_row(self, title: str) -> bool:
        target = _normalize_title(title)
        p = self.page()
        for sel in SEL_VIDEO_ROWS:
            rows = p.locator(sel)
            try:
                count = min(rows.count(), 80)
            except Exception:
                continue
            for i in range(count):
                row = rows.nth(i)
                try:
                    row_title = row.locator(SEL_ROW_TITLE).first.inner_text(timeout=500)
                except Exception:
                    try:
                        row_title = row.inner_text(timeout=500).splitlines()[0]
                    except Exception:
                        continue
                if _normalize_title(row_title) != target:
                    continue
                try:
                    row.click()
                    p.wait_for_timeout(1800)
                    log.info("opened video row for title %r", title)
                    return True
                except Exception as e:
                    log.warning("failed to open row for title %r: %s", title, e)
                    return False
        return False

    def _ensure_header_title(self, new_title: str) -> bool:
        p = self.page()
        if "/content/" not in p.url:
            return False
        if self._header_has_title(new_title):
            log.info("header title already matches: %r", new_title)
            return True

        title = self._find_any_visible_header_title()
        if not title:
            log.warning("visible header title not found on video page (url=%s)", p.url)
            return False
        try:
            before = title.inner_text(timeout=500)
        except Exception:
            before = ""
        try:
            title.click()
            p.wait_for_timeout(500)
        except Exception as e:
            log.warning("failed to click header title %r: %s", before, e)
            return False

        editor = _first(p, SEL_TITLE_EDIT_ACTIVE)
        if not editor:
            log.warning("header title editor did not appear for %r", before)
            return False

        try:
            tag_name = editor.evaluate("(el) => el.tagName.toLowerCase()")
        except Exception:
            tag_name = ""
        try:
            if tag_name in ("input", "textarea"):
                editor.fill(new_title)
            else:
                editor.press("Control+A")
                editor.press("Delete")
                editor.type(new_title, delay=20)
            editor.press("Enter")
        except Exception:
            p.keyboard.press("Control+A")
            p.keyboard.press("Delete")
            p.keyboard.type(new_title, delay=20)
            p.keyboard.press("Enter")

        p.wait_for_timeout(1200)
        if self._header_has_title(new_title):
            log.info("renamed header title from %r to %r", before, new_title)
            return True
        log.warning("header title rename did not verify for %r", new_title)
        return False

    def _find_any_visible_header_title(self) -> Locator | None:
        last_visible: Locator | None = None
        for sel in SEL_HEADER_TITLE:
            try:
                loc = self.page().locator(sel)
                count = min(loc.count(), 30)
            except Exception:
                continue
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    candidate.wait_for(state="visible", timeout=250)
                    if candidate.inner_text(timeout=500).strip():
                        last_visible = candidate
                except Exception:
                    continue
        return last_visible

    def _rename_header_title(self, current_display_title: str, new_title: str) -> bool:
        p = self.page()
        title = self._find_visible_header_title(current_display_title)
        if not title:
            return False
        try:
            title.click()
            p.wait_for_timeout(500)
        except Exception as e:
            log.warning("failed to click header title %r: %s", current_display_title, e)
            return False

        editor = _first(p, SEL_TITLE_EDIT_ACTIVE)
        if not editor:
            log.warning("header title editor did not appear for %r", current_display_title)
            return False

        try:
            tag_name = editor.evaluate("(el) => el.tagName.toLowerCase()")
        except Exception:
            tag_name = ""
        try:
            if tag_name in ("input", "textarea"):
                editor.fill(new_title)
            else:
                editor.press("Control+A")
                editor.press("Delete")
                editor.type(new_title, delay=20)
            editor.press("Enter")
        except Exception:
            p.keyboard.press("Control+A")
            p.keyboard.press("Delete")
            p.keyboard.type(new_title, delay=20)
            p.keyboard.press("Enter")

        p.wait_for_timeout(1000)
        if not self._header_has_title(new_title):
            try:
                p.mouse.click(80, 400)
                p.wait_for_timeout(700)
            except Exception:
                pass
        if self._header_has_title(new_title):
            log.info("renamed header title to: %r", new_title)
            return True
        log.warning("header title rename did not verify for %r", new_title)
        return False

    def _find_visible_header_title(self, expected_title: str) -> Locator | None:
        expected = _normalize_title(expected_title)
        for sel in SEL_HEADER_TITLE:
            try:
                loc = self.page().locator(sel)
                count = min(loc.count(), 30)
            except Exception:
                continue
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    candidate.wait_for(state="visible", timeout=250)
                    text = candidate.inner_text(timeout=500)
                except Exception:
                    continue
                if not text.strip():
                    continue
                if _normalize_title(text) == expected:
                    return candidate
        return None

    def _header_has_title(self, title: str) -> bool:
        target = _normalize_title(title)
        for sel in SEL_HEADER_TITLE:
            try:
                loc = self.page().locator(sel)
                count = min(loc.count(), 30)
            except Exception:
                continue
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    candidate.wait_for(state="visible", timeout=150)
                    if _normalize_title(candidate.inner_text(timeout=500)) == target:
                        return True
                except Exception:
                    continue
        return False

    def has_title(self, title: str) -> bool:
        space_url = self.page().url
        titles = self.list_video_titles()
        if any(_title_matches_target(existing, title) for existing in titles):
            return True
        for existing in titles:
            if not _visible_title_can_be_target(existing, title):
                continue
            if self._open_video_row(existing) and self._header_has_title(title):
                if "/space/" in space_url and "/content/" not in space_url:
                    self.open_space(space_url)
                return True
            if "/space/" in space_url and "/content/" not in space_url:
                self.open_space(space_url)
        return False

    def find_title_candidate(self, title: str) -> str | None:
        titles = self.list_video_titles()
        for existing in titles:
            if _title_matches_target(existing, title):
                return existing
        for existing in titles:
            if _visible_title_can_be_target(existing, title):
                return existing
        return None

    def find_resume_candidate(self, current_title: str, target_title: str) -> str | None:
        titles = self.list_video_titles()
        current_key = _normalize_title(current_title)
        for existing in titles:
            if current_key and _normalize_title(existing) == current_key:
                return existing
        for existing in titles:
            if _visible_title_can_be_target(existing, target_title):
                return existing
        return titles[0] if titles and not current_title else current_title or None

    def wait_for_title_in_space(self, space_url: str, title: str, timeout_s: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_s
        last_titles: list[str] = []
        while time.monotonic() < deadline:
            self.open_space(space_url, force_reload=True)
            titles = self.list_video_titles()
            last_titles = titles
            if any(_title_matches_target(existing, title) for existing in titles):
                time.sleep(self.poll_interval_s)
                self.open_space(space_url, force_reload=True)
                fresh_titles = self.list_video_titles()
                last_titles = fresh_titles
                if any(_title_matches_target(existing, title) for existing in fresh_titles):
                    log.info("verified durable title %r in target space after reload", title)
                    return
            for existing in titles:
                if not _visible_title_can_be_target(existing, title):
                    continue
                if self._open_video_row(existing) and self._header_has_title(title):
                    self.open_space(space_url, force_reload=True)
                    fresh_titles = self.list_video_titles()
                    last_titles = fresh_titles
                    if any(_visible_title_can_be_target(fresh, title) for fresh in fresh_titles):
                        log.info("verified durable title %r by opening visible row %r", title, existing)
                        return
                self.open_space(space_url, force_reload=True)
            time.sleep(self.poll_interval_s)
        raise RuntimeError(
            f"Uploaded video was not found in the target space after rename: {title!r}. "
            f"Visible titles: {last_titles[:10]!r}"
        )
