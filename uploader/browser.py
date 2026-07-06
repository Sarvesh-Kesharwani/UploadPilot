from __future__ import annotations
import logging
import threading
from pathlib import Path
from threading import Lock
from playwright.sync_api import sync_playwright, BrowserContext, Playwright

log = logging.getLogger(__name__)


class BrowserManager:
    """Owns a single persistent Chromium context. Lazy-started on first use."""

    def __init__(self, profile_dir: Path, headless: bool, slow_mo_ms: int):
        self.profile_dir = profile_dir
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self._lock = Lock()
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._owner_thread_id: int | None = None

    def _ctx_alive(self) -> bool:
        if self._ctx is None:
            return False
        try:
            # Touching .pages on a closed ctx raises TargetClosedError.
            _ = self._ctx.pages
            return True
        except Exception:
            return False

    def context(self) -> BrowserContext:
        with self._lock:
            thread_id = threading.get_ident()
            if self._ctx is not None and self._owner_thread_id != thread_id:
                log.warning(
                    "browser context belongs to another thread; refusing cross-thread reuse"
                )
                raise RuntimeError(
                    "Browser is busy in another worker. Wait for the current browser task to finish."
                )
            if self._ctx is not None and self._ctx_alive():
                return self._ctx
            # Stale/closed — tear down and relaunch.
            if self._ctx is not None:
                log.warning("browser context was closed externally; relaunching")
                try: self._ctx.close()
                except Exception: pass
                try:
                    if self._pw: self._pw.stop()
                except Exception: pass
                self._ctx = None
                self._pw = None
                self._owner_thread_id = None
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            log.info("launching chromium with profile=%s headless=%s", self.profile_dir, self.headless)
            self._ctx = self._launch_with_retry()
            self._owner_thread_id = thread_id
            return self._ctx

    def _launch_with_retry(self) -> BrowserContext:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._pw = sync_playwright().start()
                return self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                    viewport={"width": 1400, "height": 900},
                    accept_downloads=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-gpu",
                    ],
                )
            except Exception as e:
                last_error = e
                log.warning("chromium launch failed on attempt %s: %s", attempt + 1, e)
                try:
                    if self._pw:
                        self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                if attempt == 0:
                    self._remove_stale_profile_locks()
        assert last_error is not None
        raise last_error

    def _remove_stale_profile_locks(self) -> None:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile_dir / name
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
                    log.info("removed stale chromium profile lock: %s", path)
            except Exception as e:
                log.warning("could not remove chromium profile lock %s: %s", path, e)

    def shutdown(self) -> None:
        with self._lock:
            thread_id = threading.get_ident()
            if self._ctx is not None and self._owner_thread_id != thread_id:
                log.warning(
                    "browser context belongs to another thread; clearing reference without cross-thread shutdown"
                )
                self._ctx = None
                self._pw = None
                self._owner_thread_id = None
                return
            try:
                if self._ctx:
                    self._ctx.close()
            except Exception:
                pass
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._ctx = None
            self._pw = None
            self._owner_thread_id = None
