from __future__ import annotations
import logging
import threading
from pathlib import Path
from typing import Literal

from .browser import BrowserManager
from .config import Config
from .jobs import FileItem, JobStore, Status
from .youlearn import YouLearn

log = logging.getLogger(__name__)

UploadMode = Literal["sequential", "smart"]


def _is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return (
        "target page, context or browser has been closed" in text
        or "target closed" in text
        or "browser was closed" in text
    )


def _title_matches(display: str, target: str, extensions: list[str]) -> bool:
    # Exact match only. YouLearn may visually trim the extension but still
    # store the full filename — always force rename unless truly identical.
    return (display or "").strip().casefold() == (target or "").strip().casefold()


def scan_folder(folder: Path, extensions: list[str], sort: str) -> list[FileItem]:
    exts = {e.lower() for e in extensions}
    files = [p for p in folder.rglob("*")
             if p.is_file() and p.suffix.lower() in exts]
    if sort == "name_asc":
        files.sort(key=lambda p: _queue_name(folder, p).lower())
    elif sort == "name_desc":
        files.sort(key=lambda p: _queue_name(folder, p).lower(), reverse=True)
    elif sort == "mtime_asc":
        files.sort(key=lambda p: p.stat().st_mtime)
    elif sort == "mtime_desc":
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [FileItem(path=str(p), name=_queue_name(folder, p)) for p in files]


def _queue_name(root: Path, file_path: Path) -> str:
    rel = file_path.relative_to(root).as_posix()
    return rel if "/" not in rel else rel.replace("/", " / ")


class UploadWorker:
    def __init__(self, cfg: Config, store: JobStore, browser: BrowserManager):
        self.cfg = cfg
        self.store = store
        self.browser = browser
        self._thread: threading.Thread | None = None

    def run_job(self, space_url: str, folder: str, mode: UploadMode = "sequential") -> None:
        folder_p = Path(folder)
        if not folder_p.is_dir():
            raise ValueError(f"Folder not found: {folder}")
        items = scan_folder(folder_p, self.cfg.uploads.extensions, self.cfg.uploads.sort)
        if not items:
            raise ValueError("No files matched in folder")
        self.store.start(space_url, folder, mode, items)
        self._thread = threading.Thread(target=self._run, args=(space_url, mode), daemon=True)
        self._thread.start()

    def _set(self, idx: int, **changes) -> None:
        def m(job):
            for k, v in changes.items():
                setattr(job.items[idx], k, v)
        self.store.update(m)

    def _run(self, space_url: str, mode: UploadMode) -> None:
        try:
            yl = self._youlearn()
            yl.ensure_logged_in(self.cfg.auth.email, self.cfg.auth.password)
            yl.open_space(space_url, force_reload=True)

            job = self.store.current()
            assert job is not None
            if mode == "smart":
                self._run_smart(space_url, yl, job.items)
            else:
                self._run_sequential(space_url, yl, job.items)
        except Exception as e:
            log.exception("job fatal")
            # Mark all non-terminal as failed.
            def m(job):
                for it in job.items:
                    if it.status in (Status.QUEUED, Status.UPLOADING, Status.SETTLING, Status.RENAMING, Status.VALIDATING):
                        it.status = Status.FAILED
                        it.error = str(e)
            self.store.update(m)
        finally:
            self.browser.shutdown()
            self.store.finish()

    def _youlearn(self) -> YouLearn:
        return YouLearn(
            self.browser.context(),
            self.cfg.youlearn.dashboard_url,
            self.cfg.youlearn.login_url,
            self.cfg.youlearn.list_poll_interval_s,
            self.cfg.youlearn.list_poll_timeout_s,
        )

    def _relaunch(self, space_url: str) -> YouLearn | None:
        try:
            yl = self._youlearn()
            yl.ensure_logged_in(self.cfg.auth.email, self.cfg.auth.password)
            yl.open_space(space_url, force_reload=True)
            return yl
        except Exception:
            log.exception("failed to relaunch browser after closed context")
            return None

    def retry_rename(self, space_url: str, display_title: str, target_title: str, content_url: str = "") -> None:
        if self.store.current() and self.store.current().finished_at is None:
            raise RuntimeError("A job is already running")
        yl = self._youlearn()
        try:
            yl.ensure_logged_in(self.cfg.auth.email, self.cfg.auth.password)
            yl.open_space(space_url, force_reload=True)
            if yl.has_title(target_title):
                return
            if content_url and yl.rename_content_url(content_url, target_title):
                yl.wait_for_title_in_space(space_url, target_title)
                return
            try:
                yl.rename_top_video(display_title, target_title)
            except Exception:
                fallback_title = Path(target_title).stem
                if _title_matches(display_title, fallback_title, self.cfg.uploads.extensions):
                    raise
                yl.open_space(space_url, force_reload=True)
                yl.rename_top_video(fallback_title, target_title)
            yl.wait_for_title_in_space(space_url, target_title)
        finally:
            self.browser.shutdown()

    def _run_sequential(self, space_url: str, yl: YouLearn, items: list[FileItem]) -> None:
        for idx, item in enumerate(items):
            if self.store.current().cancelled:
                self._set(idx, status=Status.SKIPPED, error="cancelled")
                continue
            try:
                self._upload_rename_validate(space_url, yl, idx, item)
            except Exception as e:
                log.exception("upload failed for %s", item.path)
                self._set(idx, status=Status.FAILED, error=str(e))
                if _is_browser_closed_error(e):
                    replacement = self._relaunch(space_url)
                    if replacement:
                        yl = replacement

    def _upload_rename_validate(self, space_url: str, yl: YouLearn, idx: int, item: FileItem) -> None:
        yl.open_space(space_url, force_reload=True)
        target_title = item.name
        existing_candidate = yl.find_title_candidate(target_title)
        if existing_candidate and yl.has_title(target_title):
            self._set(
                idx,
                status=Status.SKIPPED,
                display_title=target_title,
                error="already exists in target space",
            )
            return
        if existing_candidate:
            self._set(idx, status=Status.RENAMING, display_title=existing_candidate)
            yl.rename_top_video(existing_candidate, target_title)
            self._set(idx, status=Status.VALIDATING)
            yl.wait_for_title_in_space(space_url, target_title)
            self._set(idx, status=Status.UPLOADED, display_title=target_title)
            return
        self._set(idx, status=Status.UPLOADING)
        display_title = yl.upload_one(space_url, Path(item.path))
        self._set(idx, display_title=display_title, content_url=yl.last_content_url, status=Status.SETTLING)
        display_title = yl.wait_for_latest_upload_to_settle(
            space_url,
            display_title,
            target_title,
            self.cfg.youlearn.post_upload_settle_s,
            self.cfg.youlearn.post_upload_settle_poll_s,
        )
        self._rename_and_validate(space_url, yl, idx, display_title, target_title)

    def _rename_and_validate(
        self,
        space_url: str,
        yl: YouLearn,
        idx: int,
        display_title: str,
        target_title: str,
    ) -> None:
        self._set(idx, display_title=display_title, status=Status.RENAMING)
        try:
            yl.rename_top_video(display_title, target_title)
        except Exception:
            fallback_title = Path(target_title).stem
            if _title_matches(display_title, fallback_title, self.cfg.uploads.extensions):
                raise
            try:
                yl.open_space(space_url)
                yl.rename_top_video(fallback_title, target_title)
            except Exception:
                if yl.has_title(target_title):
                    log.info("rename fallback failed but exact target title is already present")
                else:
                    raise
        self._set(idx, status=Status.VALIDATING)
        yl.wait_for_title_in_space(space_url, target_title)
        self._set(idx, status=Status.UPLOADED, display_title=target_title)

    def _run_smart(self, space_url: str, yl: YouLearn, items: list[FileItem]) -> None:
        uploaded: list[tuple[int, str, str]] = []
        for idx, item in enumerate(items):
            if self.store.current().cancelled:
                self._set(idx, status=Status.SKIPPED, error="cancelled")
                continue
            try:
                yl.open_space(space_url, force_reload=True)
                target_title = item.name
                existing_candidate = yl.find_title_candidate(target_title)
                if existing_candidate and yl.has_title(target_title):
                    self._set(
                        idx,
                        status=Status.SKIPPED,
                        display_title=target_title,
                        error="already exists in target space",
                    )
                    continue
                if existing_candidate:
                    self._set(idx, status=Status.RENAMING, display_title=existing_candidate)
                    yl.rename_top_video(existing_candidate, target_title)
                    self._set(idx, status=Status.VALIDATING)
                    yl.wait_for_title_in_space(space_url, target_title)
                    self._set(idx, status=Status.UPLOADED, display_title=target_title)
                    continue
                self._set(idx, status=Status.UPLOADING)
                display_title = yl.upload_one(space_url, Path(item.path))
                self._set(idx, display_title=display_title, content_url=yl.last_content_url, status=Status.SETTLING)
                uploaded.append((idx, display_title, target_title))
            except Exception as e:
                log.exception("batch upload failed for %s", item.path)
                self._set(idx, status=Status.FAILED, error=str(e))
                if _is_browser_closed_error(e):
                    replacement = self._relaunch(space_url)
                    if replacement:
                        yl = replacement

        if not uploaded:
            return

        settled_titles = yl.wait_for_batch_to_settle(
            space_url,
            [display for _, display, _ in uploaded],
            [target for _, _, target in uploaded],
            self.cfg.youlearn.post_upload_settle_s,
            self.cfg.youlearn.post_upload_settle_poll_s,
        )
        for pos, (idx, display_title, target_title) in enumerate(uploaded):
            if self.store.current().cancelled:
                self._set(idx, status=Status.SKIPPED, error="cancelled")
                continue
            try:
                settled_title = settled_titles[pos] if pos < len(settled_titles) else display_title
                self._set(idx, display_title=settled_title, status=Status.RENAMING)
                row_index = len(uploaded) - 1 - pos
                actual_title = yl.rename_video_at_list_index(space_url, row_index, target_title)
                self._set(idx, display_title=actual_title, status=Status.VALIDATING)
                yl.wait_for_title_in_space(space_url, target_title)
                self._set(idx, status=Status.UPLOADED, display_title=target_title)
            except Exception as e:
                log.exception("batch rename failed for %s", target_title)
                self._set(idx, status=Status.FAILED, error=str(e))
                if _is_browser_closed_error(e):
                    replacement = self._relaunch(space_url)
                    if replacement:
                        yl = replacement
