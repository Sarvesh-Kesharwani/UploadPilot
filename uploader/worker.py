from __future__ import annotations
import logging
import threading
from pathlib import Path
from typing import Literal

from .browser import BrowserManager
from .config import Config
from .history import now_iso
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


def _youlearn_ai_name(title: str, target_title: str) -> str:
    title = (title or "").strip()
    if not title:
        return ""
    return "" if title.casefold() == (target_title or "").strip().casefold() else title


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
    items: list[FileItem] = []
    for p in files:
        stat = p.stat()
        target_title = _queue_name(folder, p)
        items.append(FileItem(
            path=str(p),
            name=target_title,
            target_title=target_title,
            source_video_name=p.name,
            source_size_bytes=stat.st_size,
            source_modified_at=stat.st_mtime,
        ))
    return items


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
                        it.last_seen_at = now_iso()
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

    def retry_rename(self, space_url: str, display_title: str, target_title: str, content_url: str = "") -> dict:
        if self.store.current() and self.store.current().finished_at is None:
            raise RuntimeError("A job is already running")
        yl = self._youlearn()
        current_title = display_title
        try:
            yl.ensure_logged_in(self.cfg.auth.email, self.cfg.auth.password)
            yl.open_space(space_url, force_reload=True)
            if content_url:
                current_title = yl.current_title_for_content_url(content_url) or current_title
                if _title_matches(current_title, target_title, self.cfg.uploads.extensions):
                    return {
                        "ok": True,
                        "current_title": target_title,
                        "target_title": target_title,
                        "already_done": True,
                        "youlearn_ai_name": _youlearn_ai_name(current_title, target_title),
                    }
                if yl.rename_content_url(content_url, target_title):
                    yl.wait_for_title_in_space(space_url, target_title)
                    return {
                        "ok": True,
                        "current_title": current_title,
                        "target_title": target_title,
                        "youlearn_ai_name": _youlearn_ai_name(current_title, target_title),
                    }
                yl.open_space(space_url, force_reload=True)
            if yl.has_title(target_title):
                return {
                    "ok": True,
                    "current_title": target_title,
                    "target_title": target_title,
                    "already_done": True,
                    "youlearn_ai_name": _youlearn_ai_name(current_title, target_title),
                }
            candidate = yl.find_resume_candidate(current_title, target_title)
            if candidate:
                current_title = candidate
            if yl.has_title(target_title):
                return {
                    "ok": True,
                    "current_title": target_title,
                    "target_title": target_title,
                    "already_done": True,
                    "youlearn_ai_name": _youlearn_ai_name(current_title, target_title),
                }
            try:
                yl.rename_top_video(current_title, target_title)
            except Exception:
                fallback_title = Path(target_title).stem
                if _title_matches(current_title, fallback_title, self.cfg.uploads.extensions):
                    raise
                yl.open_space(space_url, force_reload=True)
                yl.rename_top_video(fallback_title, target_title)
                current_title = fallback_title
            yl.wait_for_title_in_space(space_url, target_title)
            return {
                "ok": True,
                "current_title": current_title,
                "target_title": target_title,
                "youlearn_ai_name": _youlearn_ai_name(current_title, target_title),
            }
        finally:
            self.browser.shutdown()

    def retry_upload(self, space_url: str, source_path: str, target_title: str) -> dict:
        if self.store.current() and self.store.current().finished_at is None:
            raise RuntimeError("A job is already running")
        file_path = Path(source_path)
        if not file_path.is_file():
            raise ValueError(f"Source video not found: {source_path}")
        if file_path.suffix.lower() not in {ext.lower() for ext in self.cfg.uploads.extensions}:
            raise ValueError(f"Source file extension is not enabled: {file_path.suffix}")

        yl = self._youlearn()
        try:
            yl.ensure_logged_in(self.cfg.auth.email, self.cfg.auth.password)
            yl.open_space(space_url, force_reload=True)
            display_title = yl.upload_one(space_url, file_path)
            content_url = yl.last_content_url
            settled_title = yl.wait_for_latest_upload_to_settle(
                space_url,
                display_title,
                target_title,
                self.cfg.youlearn.post_upload_settle_s,
                self.cfg.youlearn.post_upload_settle_poll_s,
            )
            ai_name = _youlearn_ai_name(settled_title, target_title)
            try:
                yl.rename_top_video(settled_title, target_title)
            except Exception:
                fallback_title = Path(target_title).stem
                if _title_matches(settled_title, fallback_title, self.cfg.uploads.extensions):
                    raise
                yl.open_space(space_url, force_reload=True)
                yl.rename_top_video(fallback_title, target_title)
                if not ai_name:
                    ai_name = _youlearn_ai_name(fallback_title, target_title)
            yl.wait_for_title_in_space(space_url, target_title)
            return {
                "ok": True,
                "display_title": target_title,
                "current_title": target_title,
                "target_title": target_title,
                "youlearn_ai_name": ai_name,
                "content_url": content_url,
            }
        finally:
            self.browser.shutdown()

    def continue_rename_item(self, space_url: str, display_title: str, target_title: str, content_url: str = "") -> dict:
        try:
            return self.retry_rename(space_url, display_title, target_title, content_url)
        except Exception as exc:
            return {
                "ok": False,
                "current_title": display_title,
                "target_title": target_title,
                "youlearn_ai_name": _youlearn_ai_name(display_title, target_title),
                "error": str(exc),
            }

    def _mark_uploaded_if_exists(self, idx: int, target_title: str, error: str = "") -> None:
        self._set(
            idx,
            status=Status.SKIPPED if error else Status.UPLOADED,
            display_title=target_title,
            current_title=target_title,
            error=error,
            validated_at=now_iso(),
            last_seen_at=now_iso(),
        )

    def _set_failed(self, idx: int, error: str) -> None:
        self._set(idx, status=Status.FAILED, error=error, last_seen_at=now_iso())

    def _rename_known_title(self, space_url: str, yl: YouLearn, idx: int, current_title: str, target_title: str) -> None:
        self._set(
            idx,
            status=Status.RENAMING,
            display_title=current_title,
            youlearn_ai_name=_youlearn_ai_name(current_title, target_title),
            current_title=current_title,
            rename_started_at=now_iso(),
            last_seen_at=now_iso(),
        )
        yl.rename_top_video(current_title, target_title)
        self._set(idx, status=Status.VALIDATING, current_title=target_title, last_seen_at=now_iso())
        yl.wait_for_title_in_space(space_url, target_title)
        self._set(idx, status=Status.UPLOADED, display_title=target_title, current_title=target_title, renamed_at=now_iso(), validated_at=now_iso(), last_seen_at=now_iso())

    def _run_sequential(self, space_url: str, yl: YouLearn, items: list[FileItem]) -> None:
        for idx, item in enumerate(items):
            if self.store.current().cancelled:
                self._set(idx, status=Status.SKIPPED, error="cancelled", last_seen_at=now_iso())
                continue
            try:
                self._upload_rename_validate(space_url, yl, idx, item)
            except Exception as e:
                log.exception("upload failed for %s", item.path)
                self._set(idx, status=Status.FAILED, error=str(e), last_seen_at=now_iso())
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
                current_title=target_title,
                error="already exists in target space",
                validated_at=now_iso(),
                last_seen_at=now_iso(),
            )
            return
        if existing_candidate:
            self._rename_known_title(space_url, yl, idx, existing_candidate, target_title)
            return
        self._set(idx, status=Status.UPLOADING, upload_started_at=now_iso(), last_seen_at=now_iso())
        display_title = yl.upload_one(space_url, Path(item.path))
        self._set(idx, display_title=display_title, current_title=display_title, content_url=yl.last_content_url, status=Status.SETTLING, uploaded_at=now_iso(), last_seen_at=now_iso())
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
        self._set(
            idx,
            display_title=display_title,
            youlearn_ai_name=_youlearn_ai_name(display_title, target_title),
            current_title=display_title,
            status=Status.RENAMING,
            rename_started_at=now_iso(),
            last_seen_at=now_iso(),
        )
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
        self._set(idx, status=Status.VALIDATING, current_title=target_title, last_seen_at=now_iso())
        yl.wait_for_title_in_space(space_url, target_title)
        self._set(idx, status=Status.UPLOADED, display_title=target_title, current_title=target_title, renamed_at=now_iso(), validated_at=now_iso(), last_seen_at=now_iso())

    def _run_smart(self, space_url: str, yl: YouLearn, items: list[FileItem]) -> None:
        uploaded: list[tuple[int, str, str]] = []
        for idx, item in enumerate(items):
            if self.store.current().cancelled:
                self._set(idx, status=Status.SKIPPED, error="cancelled", last_seen_at=now_iso())
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
                        current_title=target_title,
                        error="already exists in target space",
                        validated_at=now_iso(),
                        last_seen_at=now_iso(),
                    )
                    continue
                if existing_candidate:
                    self._rename_known_title(space_url, yl, idx, existing_candidate, target_title)
                    continue
                self._set(idx, status=Status.UPLOADING, upload_started_at=now_iso(), last_seen_at=now_iso())
                display_title = yl.upload_one(space_url, Path(item.path))
                self._set(idx, display_title=display_title, current_title=display_title, content_url=yl.last_content_url, status=Status.SETTLING, uploaded_at=now_iso(), last_seen_at=now_iso())
                uploaded.append((idx, display_title, target_title))
            except Exception as e:
                log.exception("batch upload failed for %s", item.path)
                self._set(idx, status=Status.FAILED, error=str(e), last_seen_at=now_iso())
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
                self._set(idx, status=Status.SKIPPED, error="cancelled", last_seen_at=now_iso())
                continue
            try:
                settled_title = settled_titles[pos] if pos < len(settled_titles) else display_title
                self._set(
                    idx,
                    display_title=settled_title,
                    youlearn_ai_name=_youlearn_ai_name(settled_title, target_title),
                    current_title=settled_title,
                    status=Status.RENAMING,
                    rename_started_at=now_iso(),
                    last_seen_at=now_iso(),
                )
                row_index = len(uploaded) - 1 - pos
                actual_title = yl.rename_video_at_list_index(space_url, row_index, target_title)
                self._set(
                    idx,
                    display_title=actual_title,
                    youlearn_ai_name=_youlearn_ai_name(actual_title, target_title),
                    current_title=target_title,
                    status=Status.VALIDATING,
                    last_seen_at=now_iso(),
                )
                yl.wait_for_title_in_space(space_url, target_title)
                self._set(idx, status=Status.UPLOADED, display_title=target_title, current_title=target_title, renamed_at=now_iso(), validated_at=now_iso(), last_seen_at=now_iso())
            except Exception as e:
                log.exception("batch rename failed for %s", target_title)
                self._set(idx, status=Status.FAILED, error=str(e), last_seen_at=now_iso())
                if _is_browser_closed_error(e):
                    replacement = self._relaunch(space_url)
                    if replacement:
                        yl = replacement
