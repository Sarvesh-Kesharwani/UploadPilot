from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import threading
from typing import Literal
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg_mod
from .browser import BrowserManager
from .drive_restore import bearer_token, list_backups, restore_backup
from .history import UploadHistoryStore
from .jobs import JobStore
from .worker import UploadWorker, scan_folder
from .youlearn import YouLearn

log = logging.getLogger(__name__)

cfg = cfg_mod.load()
history = UploadHistoryStore()
store = JobStore(on_change=history.record_job)
browser = BrowserManager(cfg.profile_path, cfg.browser.headless, cfg.browser.slow_mo_ms)
worker = UploadWorker(cfg, store, browser)
scrape_lock = threading.Lock()
scrape_browser = BrowserManager(
    cfg.profile_path.with_name(f"{cfg.profile_path.name}_scrape"),
    cfg.browser.headless,
    cfg.browser.slow_mo_ms,
)
scrape_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-scrape")
SCRAPE_TIMEOUT_S = 1800
SCRAPE_CACHE_DIR = cfg_mod.ROOT / ".state" / "scrape_cache"
SCRAPE_CACHE_INDEX = SCRAPE_CACHE_DIR / "index.json"

app = FastAPI(title="UploadPilot", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "https://uploadpilot.vercel.app",
        "https://up-uploader.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def private_network_access_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


class StartReq(BaseModel):
    space_url: str
    folder: str
    mode: Literal["sequential", "smart"] = "sequential"


class PreviewReq(BaseModel):
    folder: str


class PickFolderReq(BaseModel):
    folder: str = ""


class RestoreReq(BaseModel):
    date: str | None = None


class RetryRenameReq(BaseModel):
    space_url: str
    display_title: str
    target_title: str
    youlearn_ai_name: str = ""
    content_url: str = ""
    record_id: str = ""
    job_id: str = ""
    item_index: int | None = None
    source_path: str = ""
    folder: str = ""
    mode: str = ""


class RetryUploadReq(BaseModel):
    space_url: str
    source_path: str
    target_title: str
    display_title: str = ""
    youlearn_ai_name: str = ""
    content_url: str = ""
    record_id: str = ""
    job_id: str = ""
    item_index: int | None = None
    folder: str = ""
    mode: str = ""


class HistoryImportReq(BaseModel):
    data: dict


class ScrapeSummaryReq(BaseModel):
    url: str
    scope: Literal["auto", "video", "space"] = "auto"
    max_videos: int = 0


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/scrape")
def scrape_page():
    return FileResponse(STATIC / "scrape.html")


@app.get("/api/config")
def app_config():
    return {
        "appName": "UploadPilot",
        "timezone": "Asia/Calcutta",
        "primarySyncFile": "UploadPilot-data.json",
        "backupFolder": "UploadPilot-backups",
        "googleClientId": cfg.auth.google_client_id,
    }


@app.get("/api/state")
def state():
    return store.snapshot()


@app.get("/api/history")
def upload_history():
    return history.snapshot()


@app.get("/api/scrape/cache")
def scrape_cache_list():
    return {"items": _read_scrape_cache()}


@app.get("/api/scrape/cache/{entry_id}/download")
def scrape_cache_download(entry_id: str):
    entry = _cache_entry(entry_id)
    if not entry:
        raise HTTPException(404, "Cached summary not found")
    path = SCRAPE_CACHE_DIR / entry["filename"]
    if not path.exists():
        raise HTTPException(404, "Cached summary file missing")
    media_type = "application/json" if path.suffix.lower() == ".json" else "text/markdown"
    return FileResponse(path, media_type=media_type, filename=entry["filename"])


@app.post("/api/scrape/summary")
def scrape_summary(req: ScrapeSummaryReq):
    target_url = req.url.strip()
    if not target_url:
        raise HTTPException(400, "URL is required")
    if store.current() and store.current().finished_at is None:
        raise HTTPException(409, "A job is already running")
    if not scrape_lock.acquire(blocking=False):
        raise HTTPException(409, "A scrape is already running")
    try:
        result = scrape_executor.submit(_scrape_summary_sync, target_url, req.scope, req.max_videos).result(
            timeout=SCRAPE_TIMEOUT_S
        )
        _clean_scrape_result(result)
        cache_entry = _save_scrape_result(result)
        result["cache"] = cache_entry
        return result
    except FutureTimeout:
        log.error("summary scrape timed out after %ss", SCRAPE_TIMEOUT_S)
        raise HTTPException(504, f"Summary scrape timed out after {SCRAPE_TIMEOUT_S} seconds")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("summary scrape failed")
        raise HTTPException(500, str(e))
    finally:
        scrape_lock.release()


def _scrape_summary_sync(target_url: str, requested_scope: str, max_videos: int) -> dict:
    yl = YouLearn(
        scrape_browser.context(),
        cfg.youlearn.dashboard_url,
        cfg.youlearn.login_url,
        cfg.youlearn.list_poll_interval_s,
        cfg.youlearn.list_poll_timeout_s,
    )
    yl.ensure_logged_in(cfg.auth.email, cfg.auth.password)
    scope = requested_scope
    if scope == "auto":
        scope = "space" if "/space/" in target_url and "/content/" not in target_url else "video"
    if scope == "space":
        items = yl.scrape_space_summaries(target_url, max(0, max_videos))
        return {
            "ok": True,
            "scope": "space",
            "url": target_url,
            "count": len(items),
            "items": items,
        }
    item = yl.scrape_video_summary(target_url)
    return {
        "ok": True,
        "scope": "video",
        "url": target_url,
        "summary": item["summary"],
        "item": item,
    }


def _read_scrape_cache() -> list[dict]:
    try:
        data = json.loads(SCRAPE_CACHE_INDEX.read_text(encoding="utf-8-sig"))
        items = data.get("items", [])
        return items if isinstance(items, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        log.warning("scrape cache index is unreadable", exc_info=True)
        return []


def _write_scrape_cache(items: list[dict]) -> None:
    SCRAPE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SCRAPE_CACHE_INDEX.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cache_entry(entry_id: str) -> dict | None:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", entry_id or ""):
        return None
    for entry in _read_scrape_cache():
        if entry.get("id") == entry_id:
            return entry
    return None


def _save_scrape_result(result: dict) -> dict:
    items = result.get("items") or ([result.get("item")] if result.get("item") else [])
    items = [item for item in items if isinstance(item, dict) and not item.get("error")]
    if not items:
        raise RuntimeError("No summary was extracted to save")
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    title = _cache_title(result, items)
    markdown = _result_to_markdown(result, items, created_at)
    entry_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slugify(title)[:56]}"
    SCRAPE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if result.get("scope") == "space":
        filename = f"{entry_id}.json"
        file_format = "json"
        json_payload = _result_to_json_payload(result, items, created_at)
        (SCRAPE_CACHE_DIR / filename).write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        filename = f"{entry_id}.md"
        file_format = "markdown"
        (SCRAPE_CACHE_DIR / filename).write_text(markdown, encoding="utf-8")
    entry = {
        "id": entry_id,
        "title": title,
        "scope": result.get("scope", ""),
        "format": file_format,
        "source_url": result.get("url", ""),
        "filename": filename,
        "download_url": f"/api/scrape/cache/{entry_id}/download",
        "created_at": created_at,
        "count": len(items),
        "excerpt": _excerpt(" ".join(item.get("summary", "") for item in items)),
    }
    cache = _read_scrape_cache()
    cache.insert(0, entry)
    _write_scrape_cache(cache[:100])
    cached = {**entry, "markdown": markdown}
    if result.get("scope") == "space":
        cached["json"] = json_payload
    return cached


def _clean_scrape_result(result: dict) -> None:
    items = result.get("items") or ([result.get("item")] if result.get("item") else [])
    for item in items:
        if not isinstance(item, dict) or item.get("error"):
            continue
        item["summary"] = _summary_markdown(item.get("summary", ""))
    if result.get("item"):
        result["summary"] = result["item"].get("summary", "")


def _cache_title(result: dict, items: list[dict]) -> str:
    if result.get("scope") == "space":
        return f"Space summary ({len(items)} videos)"
    return (items[0].get("title") or "Video summary").strip()


def _result_to_markdown(result: dict, items: list[dict], created_at: str) -> str:
    title = _cache_title(result, items)
    parts = [
        f"# {_md_text(title)}",
        "",
        f"- Source: {result.get('url', '')}",
        f"- Scope: {result.get('scope', '')}",
        f"- Extracted: {created_at}",
        "",
    ]
    for item in items:
        item_title = _md_text(item.get("title") or "Untitled video")
        if result.get("scope") == "space":
            parts.extend([f"## {item_title}", ""])
        elif item_title and item_title != title:
            parts.extend([f"## {item_title}", ""])
        if item.get("url"):
            parts.extend([f"Source video: {item['url']}", ""])
        parts.extend([_summary_markdown(item.get("summary", "")), ""])
    return "\n".join(parts).strip() + "\n"


def _result_to_json_payload(result: dict, items: list[dict], created_at: str) -> dict:
    return {
        "space_url": result.get("url", ""),
        "created_at": created_at,
        "count": len(items),
        "videos": [
            {
                "title": _md_text(item.get("title") or "Untitled video"),
                "source_url": item.get("url", ""),
                "markdown_summary": _summary_markdown(item.get("summary", "")),
            }
            for item in items
        ],
    }


def _summary_markdown(text: str) -> str:
    cleaned = []
    for raw in (text or "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        line = re.sub(r"^[•·]\s*", "- ", line)
        line = re.sub(r"^[-*]\s+", "- ", line)
        cleaned.append(line)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned)


def _summary_markdown(text: str) -> str:
    text = _strip_css_noise(text or "")
    cleaned = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if _is_css_noise_line(line):
            continue
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        line = re.sub(r"^\+\d+\s*", "", line)
        line = re.sub(r"^[•·]\s*", "- ", line)
        line = re.sub(r"^[-*]\s+", "- ", line)
        line = re.sub(r"\s*(\d{1,2}:\d{2})\s*\.\s*", r" \1\n\n", line)
        cleaned.append(line)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def _strip_css_noise(text: str) -> str:
    text = re.sub(r"(?s)/\*.*?\*/", "\n", text)
    text = re.sub(r"(?ms)^\s*\.[^{\n]+\{.*?^\s*\}\s*", "\n", text)
    return text


def _is_css_noise_line(line: str) -> bool:
    if not line:
        return False
    css_prefixes = (
        ".mermaid-container",
        "#mermaid_",
        "@keyframes",
        "fill:",
        "stroke:",
        "color:",
        "background:",
        "border:",
        "font-",
    )
    return (
        line in {"{", "}"}
        or line.startswith(css_prefixes)
        or "#mermaid_" in line
        or "@keyframes" in line
        or "{font-family:" in line
        or bool(re.match(r"^[a-zA-Z-]+:\s*[^;]+;?$", line))
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "summary"


def _excerpt(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _md_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip() or "Untitled"


@app.post("/api/folder/pick")
def pick_folder(req: PickFolderReq):
    initial = req.folder.strip() if req.folder else str(Path.home() / "Videos")
    try:
        import tkinter.filedialog, tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = tkinter.filedialog.askdirectory(initialdir=initial, title="Choose videos folder")
        root.destroy()
    except Exception:
        return {"folder": "", "error": "Folder picker not available in this environment"}
    if not chosen:
        return {"folder": ""}
    return {"folder": str(Path(chosen))}


@app.post("/api/preview")
def preview(req: PreviewReq):
    try:
        items = scan_folder(Path(req.folder), cfg.uploads.extensions, cfg.uploads.sort)
    except FileNotFoundError:
        raise HTTPException(400, "Folder does not exist")
    except NotADirectoryError:
        raise HTTPException(400, "Path is not a directory")
    return {"count": len(items), "files": [it.name for it in items]}


@app.post("/api/start")
def start(req: StartReq):
    try:
        worker.run_job(req.space_url.strip(), req.folder.strip(), req.mode)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "state": store.snapshot()}


@app.post("/api/cancel")
def cancel():
    store.cancel()
    return {"ok": True}


@app.post("/api/rename")
def retry_rename(req: RetryRenameReq):
    if store.current() and store.current().finished_at is None:
        raise HTTPException(409, "A job is already running")
    
    request_id = req.record_id or f"{req.job_id}:{req.item_index}" if req.item_index is not None else ""
    rid = worker.rename_queue.enqueue(
        req.space_url.strip(),
        req.display_title.strip(),
        req.target_title.strip(),
        req.content_url.strip(),
        request_id,
    )
    history.record_rename_attempt(req.model_dump(), {"ok": True, "queued": True, "request_id": rid})
    return {"ok": True, "queued": True, "request_id": rid, "queue_snapshot": worker.rename_queue.snapshot()}


@app.post("/api/reupload")
async def retry_upload(req: RetryUploadReq):
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _do_reupload, req)
        history.record_upload_attempt(req.model_dump(), result)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    return result


def _do_reupload(req: RetryUploadReq) -> dict:
    try:
        return worker.retry_upload(
            req.space_url.strip(),
            req.source_path.strip(),
            req.target_title.strip(),
        )
    except RuntimeError:
        raise
    except ValueError:
        raise


@app.get("/api/rename/queue")
def rename_queue_status():
    return worker.rename_queue.snapshot()


@app.post("/api/history/import")
def import_history(req: HistoryImportReq):
    return history.merge_client_data(req.data)


@app.post("/api/history/clear-last-batch")
def clear_history_last_batch():
    return history.clear_last_batch()


@app.get("/api/drive/restore")
def drive_restore_get(request: Request):
    token = bearer_token(request)
    return list_backups(token)


@app.post("/api/drive/restore")
def drive_restore_post(req: RestoreReq, request: Request):
    token = bearer_token(request)
    return restore_backup(token, req.date)


@app.get("/api/events")
async def events(request: Request):
    q = store.subscribe()
    loop = asyncio.get_event_loop()

    async def gen():
        try:
            yield f"data: {json.dumps(store.snapshot())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await loop.run_in_executor(None, q.get, True, 15)
                    yield f"data: {json.dumps(item)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            store.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.on_event("shutdown")
def _shutdown():
    browser.shutdown()
    try:
        scrape_executor.submit(scrape_browser.shutdown).result(timeout=10)
    except Exception:
        log.warning("scrape browser did not shut down cleanly", exc_info=True)
    scrape_executor.shutdown(wait=False, cancel_futures=True)
