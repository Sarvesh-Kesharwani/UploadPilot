from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ROOT
from .jobs import Job

STATE_DIR = ROOT / ".state"
HISTORY_FILE = STATE_DIR / "upload_history.json"
TZ = timezone(timedelta(hours=5, minutes=30), name="Asia/Calcutta")


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def today_key() -> str:
    return datetime.now(TZ).date().isoformat()


def space_path(space_url: str) -> str:
    parsed = urlsplit(space_url or "")
    return parsed.path.strip("/")


def youlearn_ai_name(current_title: str, target_title: str, stored_name: str = "") -> str:
    stored_name = (stored_name or "").strip()
    if stored_name:
        return stored_name
    current_title = (current_title or "").strip()
    if not current_title:
        return ""
    return "" if current_title.casefold() == (target_title or "").strip().casefold() else current_title


class UploadHistoryStore:
    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def record_job(self, job: Job) -> None:
        with self._lock:
            data = self._load()
            data["last_batch"] = self._job_to_dict(job)
            data["updated_at"] = now_iso()

            records = data.setdefault("records", {})
            daily = data.setdefault("daily_uploads", {})
            for index, item in enumerate(job.items):
                item_data = item.to_dict()
                record = self._record_from_item(job, index, item_data)
                existing = records.get(record["record_id"], {})
                record["first_seen_at"] = existing.get("first_seen_at") or record["first_seen_at"]
                records[record["record_id"]] = {**existing, **record}

                if record.get("uploaded_at") or record.get("content_url") or record.get("current_title"):
                    day_records = daily.setdefault(record["day"], {})
                    day_records[record["record_id"]] = records[record["record_id"]]

            self._save(data)

    def record_rename_attempt(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        with self._lock:
            data = self._load()
            data["updated_at"] = now_iso()
            records = data.setdefault("records", {})
            daily = data.setdefault("daily_uploads", {})
            record_id = payload.get("record_id") or self._record_id(
                payload.get("job_id", ""),
                payload.get("item_index", -1),
                payload.get("source_path", ""),
                payload.get("target_title", ""),
                payload.get("space_url", ""),
            )
            record = {
                "schema_version": 1,
                "record_id": record_id,
                "job_id": payload.get("job_id", ""),
                "item_index": payload.get("item_index"),
                "day": today_key(),
                "first_seen_at": now_iso(),
                "last_seen_at": now_iso(),
                "space_url": payload.get("space_url", ""),
                "space_path": space_path(payload.get("space_url", "")),
                "folder": payload.get("folder", ""),
                "mode": payload.get("mode", ""),
                "source_path": payload.get("source_path", ""),
                "source_video_name": Path(payload.get("source_path", "")).name,
                "target_title": payload.get("target_title", ""),
                "current_title": result.get("current_title") or payload.get("display_title", ""),
                "youlearn_display_title": result.get("current_title") or payload.get("display_title", ""),
                "youlearn_ai_name": youlearn_ai_name(
                    result.get("youlearn_ai_name") or result.get("current_title") or payload.get("display_title", ""),
                    payload.get("target_title", ""),
                    payload.get("youlearn_ai_name", ""),
                ),
                "content_url": payload.get("content_url", ""),
                "status": "uploaded" if result.get("ok") else "failed",
                "renamed_at": now_iso() if result.get("ok") else "",
                "validated_at": now_iso() if result.get("ok") else "",
                "error": result.get("error", ""),
                "resume_attempted_at": now_iso(),
            }
            existing = records.get(record_id, {})
            record["first_seen_at"] = existing.get("first_seen_at") or record["first_seen_at"]
            records[record_id] = {**existing, **record}
            daily.setdefault(record["day"], {})[record_id] = records[record_id]
            self._save(data)

    def record_upload_attempt(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        with self._lock:
            data = self._load()
            data["updated_at"] = now_iso()
            records = data.setdefault("records", {})
            daily = data.setdefault("daily_uploads", {})
            record_id = payload.get("record_id") or self._record_id(
                payload.get("job_id", ""),
                payload.get("item_index", -1),
                payload.get("source_path", ""),
                payload.get("target_title", ""),
                payload.get("space_url", ""),
            )
            now = now_iso()
            record = {
                "schema_version": 1,
                "record_id": record_id,
                "job_id": payload.get("job_id", ""),
                "item_index": payload.get("item_index"),
                "day": today_key(),
                "first_seen_at": now,
                "last_seen_at": now,
                "space_url": payload.get("space_url", ""),
                "space_path": space_path(payload.get("space_url", "")),
                "folder": payload.get("folder", ""),
                "mode": payload.get("mode", ""),
                "source_path": payload.get("source_path", ""),
                "source_video_name": Path(payload.get("source_path", "")).name,
                "target_title": payload.get("target_title", ""),
                "current_title": result.get("current_title") or payload.get("display_title", ""),
                "youlearn_display_title": result.get("display_title") or result.get("current_title") or "",
                "youlearn_ai_name": youlearn_ai_name(
                    result.get("youlearn_ai_name") or result.get("current_title") or payload.get("display_title", ""),
                    payload.get("target_title", ""),
                    payload.get("youlearn_ai_name", ""),
                ),
                "content_url": result.get("content_url") or payload.get("content_url", ""),
                "status": "uploaded" if result.get("ok") else "failed",
                "uploaded_at": now if result.get("ok") else "",
                "renamed_at": now if result.get("ok") else "",
                "validated_at": now if result.get("ok") else "",
                "error": result.get("error", ""),
                "reupload_attempted_at": now,
            }
            existing = records.get(record_id, {})
            record["first_seen_at"] = existing.get("first_seen_at") or record["first_seen_at"]
            records[record_id] = {**existing, **record}
            daily.setdefault(record["day"], {})[record_id] = records[record_id]
            self._save(data)

    def merge_client_data(self, client_data: dict[str, Any]) -> dict[str, Any]:
        incoming = client_data.get("upload_history") if isinstance(client_data, dict) else None
        if not isinstance(incoming, dict):
            return self.snapshot()
        with self._lock:
            data = self._load()
            records = data.setdefault("records", {})
            for record_id, record in (incoming.get("records") or {}).items():
                if isinstance(record, dict):
                    existing = records.get(record_id, {})
                    records[record_id] = {**record, **existing}
            for day, day_records in (incoming.get("daily_uploads") or {}).items():
                if not isinstance(day_records, dict):
                    continue
                bucket = data.setdefault("daily_uploads", {}).setdefault(day, {})
                for record_id, record in day_records.items():
                    if isinstance(record, dict):
                        bucket[record_id] = {**record, **bucket.get(record_id, {})}
            if isinstance(incoming.get("last_batch"), dict) and not data.get("last_batch"):
                data["last_batch"] = incoming["last_batch"]
            data["updated_at"] = now_iso()
            self._save(data)
            return data

    def clear_last_batch(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            data["last_batch"] = None
            data["updated_at"] = now_iso()
            self._save(data)
            return data

    def _record_from_item(self, job: Job, index: int, item: dict[str, Any]) -> dict[str, Any]:
        target_title = item.get("target_title") or item.get("name") or ""
        source_path = item.get("path") or ""
        current_title = item.get("current_title") or item.get("display_title") or ""
        record_id = self._record_id(job.id, index, source_path, target_title, job.space_url)
        last_seen = item.get("last_seen_at") or now_iso()
        return {
            "schema_version": 1,
            "record_id": record_id,
            "job_id": job.id,
            "item_index": index,
            "day": self._record_day(item),
            "first_seen_at": item.get("queued_at") or job.started_at,
            "last_seen_at": last_seen,
            "space_url": job.space_url,
            "space_path": space_path(job.space_url),
            "folder": job.folder,
            "mode": job.mode,
            "source_path": source_path,
            "source_video_name": item.get("source_video_name") or Path(source_path).name,
            "source_size_bytes": item.get("source_size_bytes", 0),
            "source_modified_at": item.get("source_modified_at", 0),
            "target_title": target_title,
            "current_title": current_title,
            "youlearn_display_title": item.get("display_title") or "",
            "youlearn_ai_name": youlearn_ai_name(current_title, target_title, item.get("youlearn_ai_name") or ""),
            "content_url": item.get("content_url") or "",
            "progress_percent": item.get("progress_percent") or 0,
            "status": item.get("status") or "",
            "queued_at": item.get("queued_at") or "",
            "upload_started_at": item.get("upload_started_at") or "",
            "uploaded_at": item.get("uploaded_at") or "",
            "rename_started_at": item.get("rename_started_at") or "",
            "renamed_at": item.get("renamed_at") or "",
            "validated_at": item.get("validated_at") or "",
            "error": item.get("error") or "",
        }

    def _job_to_dict(self, job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "space_url": job.space_url,
            "space_path": space_path(job.space_url),
            "folder": job.folder,
            "mode": job.mode,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "cancelled": job.cancelled,
            "savedAt": int(datetime.now(TZ).timestamp() * 1000),
            "items": [item.to_dict() for item in job.items],
        }

    def _record_day(self, item: dict[str, Any]) -> str:
        for key in ("uploaded_at", "rename_started_at", "validated_at", "queued_at"):
            value = item.get(key)
            if isinstance(value, str) and value:
                try:
                    return datetime.fromisoformat(value).astimezone(TZ).date().isoformat()
                except ValueError:
                    continue
        return today_key()

    def _record_id(self, job_id: str, index: int, source_path: str, target_title: str, space_url: str) -> str:
        if job_id and index is not None and int(index) >= 0:
            return f"{job_id}:{index}"
        return f"{space_path(space_url)}|{source_path}|{target_title}"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("schema_version", 1)
        data.setdefault("updated_at", "")
        data.setdefault("last_batch", None)
        data.setdefault("records", {})
        data.setdefault("daily_uploads", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "updated_at": "",
            "last_batch": None,
            "records": {},
            "daily_uploads": {},
        }
