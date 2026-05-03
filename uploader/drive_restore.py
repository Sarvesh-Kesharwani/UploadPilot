from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

APP_NAME = "UploadPilot"
TIMEZONE = "Asia/Calcutta"
PRIMARY_FILE = f"{APP_NAME}-data.json"
BACKUP_FOLDER = f"{APP_NAME}-backups"
JSON_MIME = "application/json"
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Signed out: Google access token required")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Signed out: Google access token required")
    return token


def yesterday_local() -> str:
    return (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=1)).strftime("%Y-%m-%d")


def _headers(token: str, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _request(
    token: str,
    method: str,
    url: str,
    body: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    req = urllib.request.Request(url, data=body, method=method, headers=_headers(token, content_type))
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise HTTPException(401, f"Google token rejected or expired: {detail}")
        raise HTTPException(exc.code, detail or exc.reason)
    if not raw:
        return None
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _query_files(token: str, query: str, fields: str = "files(id,name,createdTime,modifiedTime)") -> list[dict]:
    params = urllib.parse.urlencode({
        "spaces": "appDataFolder",
        "q": query,
        "fields": fields,
        "pageSize": "1000",
    })
    data = _request(token, "GET", f"{DRIVE_API}/files?{params}")
    return data.get("files", []) if isinstance(data, dict) else []


def _find_primary(token: str) -> dict | None:
    files = _query_files(
        token,
        f"name = '{PRIMARY_FILE}' and trashed = false",
        "files(id,name,modifiedTime)",
    )
    return files[0] if files else None


def _find_backup_folder(token: str) -> dict | None:
    files = _query_files(
        token,
        f"name = '{BACKUP_FOLDER}' and mimeType = '{FOLDER_MIME}' and trashed = false",
        "files(id,name,createdTime)",
    )
    return files[0] if files else None


def _download_json(token: str, file_id: str) -> dict:
    data = _request(token, "GET", f"{DRIVE_API}/files/{file_id}?alt=media")
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid Drive payload")
    validate_payload(data)
    return data


def validate_payload(payload: dict) -> None:
    if payload.get("app") != APP_NAME or payload.get("schemaVersion") != 1:
        raise HTTPException(400, "Invalid Drive payload")
    if "savedAt" not in payload or "data" not in payload:
        raise HTTPException(400, "Invalid Drive payload")


def _multipart_body(metadata: dict, payload: dict) -> tuple[bytes, str]:
    boundary = "uploadpilot_boundary"
    parts = [
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n",
        json.dumps(metadata, separators=(",", ":")),
        f"\r\n--{boundary}\r\nContent-Type: {JSON_MIME}\r\n\r\n",
        json.dumps(payload, separators=(",", ":")),
        f"\r\n--{boundary}--\r\n",
    ]
    return "".join(parts).encode("utf-8"), f"multipart/related; boundary={boundary}"


def _create_json_file(token: str, name: str, payload: dict, parents: list[str]) -> dict:
    metadata = {"name": name, "mimeType": JSON_MIME, "parents": parents}
    body, content_type = _multipart_body(metadata, payload)
    return _request(
        token,
        "POST",
        f"{DRIVE_UPLOAD_API}/files?uploadType=multipart&fields=id,name,createdTime",
        body,
        content_type,
    )


def _update_json_file(token: str, file_id: str, name: str, payload: dict) -> dict:
    metadata = {"name": name, "mimeType": JSON_MIME}
    body, content_type = _multipart_body(metadata, payload)
    return _request(
        token,
        "PATCH",
        f"{DRIVE_UPLOAD_API}/files/{file_id}?uploadType=multipart&fields=id,name,modifiedTime",
        body,
        content_type,
    )


def _create_folder(token: str, name: str) -> dict:
    body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": ["appDataFolder"]}).encode("utf-8")
    return _request(
        token,
        "POST",
        f"{DRIVE_API}/files?fields=id,name",
        body,
        "application/json",
    )


def ensure_backup_folder(token: str) -> dict:
    return _find_backup_folder(token) or _create_folder(token, BACKUP_FOLDER)


def list_backups(token: str) -> dict:
    folder = _find_backup_folder(token)
    backups: list[dict] = []
    if folder:
        files = _query_files(
            token,
            f"'{folder['id']}' in parents and trashed = false",
            "files(id,name,createdTime,modifiedTime)",
        )
        backups = sorted(files, key=lambda f: f.get("name", ""), reverse=True)
    default_date = yesterday_local()
    return {
        "backups": backups,
        "defaultDate": default_date,
        "hasYesterdayBackup": _has_backup_for_date(backups, default_date),
    }


def restore_backup(token: str, date: str | None = None) -> dict:
    target_date = _validate_date(date or yesterday_local())
    folder = _find_backup_folder(token)
    if not folder:
        raise HTTPException(404, f"No backup found for {target_date}")
    backups = _query_files(
        token,
        f"'{folder['id']}' in parents and trashed = false",
        "files(id,name,createdTime,modifiedTime)",
    )
    selected = _select_backup(backups, target_date)
    if not selected:
        raise HTTPException(404, f"No backup found for {target_date}")
    backup_payload = _download_json(token, selected["id"])

    primary = _find_primary(token)
    if primary:
        current_payload = _download_json(token, primary["id"])
        snapshot_name = f"{APP_NAME}-snapshot-{_snapshot_stamp()}.json"
        _create_json_file(token, snapshot_name, current_payload, [folder["id"]])
        _update_json_file(token, primary["id"], PRIMARY_FILE, backup_payload)
    else:
        _create_json_file(token, PRIMARY_FILE, backup_payload, ["appDataFolder"])

    return {
        "restored": backup_payload,
        "backup": selected,
        "date": target_date,
        "counts": _payload_counts(backup_payload),
    }


def _select_backup(backups: list[dict], date: str) -> dict | None:
    daily = f"{APP_NAME}-state-{date}.json"
    for item in backups:
        if item.get("name") == daily:
            return item
    snapshots = [
        item for item in backups
        if item.get("name", "").startswith(f"{APP_NAME}-snapshot-{date}T")
    ]
    return sorted(snapshots, key=lambda item: item.get("name", ""), reverse=True)[0] if snapshots else None


def _has_backup_for_date(backups: list[dict], date: str) -> bool:
    return _select_backup(backups, date) is not None


def _validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise HTTPException(400, "Invalid restore date. Use YYYY-MM-DD.")
    return value


def _snapshot_stamp() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds").replace(":", "-") + "Z"


def _payload_counts(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict):
        return {"keys": len(data)}
    if isinstance(data, list):
        return {"items": len(data)}
    return {}
