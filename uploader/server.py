from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
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

log = logging.getLogger(__name__)

cfg = cfg_mod.load()
history = UploadHistoryStore()
store = JobStore(on_change=history.record_job)
browser = BrowserManager(cfg.profile_path, cfg.browser.headless, cfg.browser.slow_mo_ms)
worker = UploadWorker(cfg, store, browser)

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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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
