from __future__ import annotations
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
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


@dataclass
class FileItem:
    path: str
    name: str                       # original filename (what we rename TO)
    status: Status = Status.QUEUED
    display_title: str = ""         # title YouLearn auto-assigned after upload
    content_url: str = ""           # YouLearn content URL when upload exposes one
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
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

    def __init__(self):
        self._lock = threading.Lock()
        self._current: Optional[Job] = None
        self._subs: list[queue.Queue] = []

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
        self._publish()
        return job

    def current(self) -> Optional[Job]:
        return self._current

    def update(self, mutator: Callable[[Job], None]) -> None:
        with self._lock:
            if self._current:
                mutator(self._current)
        self._publish()

    def cancel(self) -> None:
        def m(j: Job):
            j.cancelled = True
        self.update(m)

    def finish(self) -> None:
        def m(j: Job):
            j.finished_at = time.time()
        self.update(m)
