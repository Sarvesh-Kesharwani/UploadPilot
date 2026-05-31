from __future__ import annotations
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class Status(str, Enum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    SETTLING = "settling"
    RENAMING = "renaming"
    VALIDATING = "validating"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED = "skipped"


STATUS_PROGRESS = {
    Status.QUEUED: 0,
    Status.UPLOADING: 25,
    Status.SETTLING: 55,
    Status.RENAMING: 75,
    Status.VALIDATING: 90,
    Status.UPLOADED: 100,
    Status.SKIPPED: 100,
    Status.FAILED: 0,
}


@dataclass
class FileItem:
    path: str
    name: str                       # original filename (what we rename TO)
    status: Status = Status.QUEUED
    display_title: str = ""         # title YouLearn auto-assigned after upload
    youlearn_ai_name: str = ""      # title YouLearn generated before our rename
    current_title: str = ""         # latest title checked on YouLearn
    target_title: str = ""          # explicit copy of the desired YouLearn title
    source_video_name: str = ""     # original local file name
    source_size_bytes: int = 0
    source_modified_at: float = 0.0
    content_url: str = ""           # YouLearn content URL when upload exposes one
    progress_percent: int = 0
    error: str = ""
    queued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    upload_started_at: str = ""
    uploaded_at: str = ""
    rename_started_at: str = ""
    renamed_at: str = ""
    validated_at: str = ""
    last_seen_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["target_title"] = self.target_title or self.name
        d["source_video_name"] = self.source_video_name or Path(self.path).name
        d["current_title"] = self.current_title or self.display_title
        d["progress_percent"] = self.progress_percent or STATUS_PROGRESS.get(self.status, 0)
        return d


@dataclass
class Job:
    id: str
    space_url: str
    folder: str
    mode: str
    items: list[FileItem]
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancelled: bool = False


class JobStore:
    """Single-job-at-a-time. Subscribers get pushed updates via callbacks."""

    def __init__(self, on_change: Callable[[Job], None] | None = None):
        self._lock = threading.Lock()
        self._current: Optional[Job] = None
        self._subs: list[queue.Queue] = []
        self._on_change = on_change

    # -- pub/sub ---------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        seed = None
        with self._lock:
            self._subs.append(q)
            # Seed with current state.
            if self._current:
                j = self._current
                seed = {
                    "job": {
                        "id": j.id,
                        "space_url": j.space_url,
                        "folder": j.folder,
                        "mode": j.mode,
                        "started_at": j.started_at,
                        "finished_at": j.finished_at,
                        "cancelled": j.cancelled,
                        "items": [it.to_dict() for it in j.items],
                    }
                }
        if seed:
            q.put(seed)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self) -> None:
        snap = self.snapshot()
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass

    def _record_change(self, job: Job | None) -> None:
        if not job or not self._on_change:
            return
        try:
            self._on_change(job)
        except Exception:
            log.exception("failed to persist job state")

    # -- state -----------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            if not self._current:
                return {"job": None}
            j = self._current
            return {
                "job": {
                    "id": j.id,
                    "space_url": j.space_url,
                    "folder": j.folder,
                    "mode": j.mode,
                    "started_at": j.started_at,
                    "finished_at": j.finished_at,
                    "cancelled": j.cancelled,
                    "items": [it.to_dict() for it in j.items],
                }
            }

    def start(self, space_url: str, folder: str, mode: str, items: list[FileItem]) -> Job:
        with self._lock:
            if self._current and self._current.finished_at is None:
                raise RuntimeError("A job is already running")
            self._current = Job(id=uuid.uuid4().hex[:8], space_url=space_url,
                                folder=folder, mode=mode, items=items)
            job = self._current
        self._record_change(job)
        self._publish()
        return job

    def current(self) -> Optional[Job]:
        return self._current

    def update(self, mutator: Callable[[Job], None]) -> None:
        job = None
        with self._lock:
            if self._current:
                mutator(self._current)
                job = self._current
        self._record_change(job)
        self._publish()

    def cancel(self) -> None:
        def m(j: Job):
            j.cancelled = True
        self.update(m)

    def finish(self) -> None:
        def m(j: Job):
            j.finished_at = time.time()
        self.update(m)
