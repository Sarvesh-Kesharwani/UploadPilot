from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg_mod
from .browser import BrowserManager
from .drive_restore import bearer_token, list_backups, restore_backup
from .jobs import JobStore
from .worker import UploadWorker, scan_folder

log = logging.getLogger(__name__)

cfg = cfg_mod.load()
store = JobStore()
browser = BrowserManager(cfg.profile_path, cfg.browser.headless, cfg.browser.slow_mo_ms)
worker = UploadWorker(cfg, store, browser)

app = FastAPI(title="UploadPilot", version="0.1")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class StartReq(BaseModel):
    space_url: str
    folder: str
    mode: Literal["sequential", "smart"] = "sequential"


class PreviewReq(BaseModel):
    folder: str


class RestoreReq(BaseModel):
    date: str | None = None


class RetryRenameReq(BaseModel):
    space_url: str
    display_title: str
    target_title: str
    content_url: str = ""


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
    try:
        worker.retry_rename(
            req.space_url.strip(),
            req.display_title.strip(),
            req.target_title.strip(),
            req.content_url.strip(),
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


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
            # initial snapshot
            yield f"data: {json.dumps(store.snapshot())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await loop.run_in_executor(None, q.get, True, 15)
                    yield f"data: {json.dumps(item)}\n\n"
                except Exception:
                    # keep-alive ping
                    yield ": ping\n\n"
        finally:
            store.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.on_event("shutdown")
def _shutdown():
    browser.shutdown()
