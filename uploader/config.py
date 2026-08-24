from __future__ import annotations
import os
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", encoding="utf-8-sig")


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class BrowserCfg(BaseModel):
    profile_dir: str = ".browser_profile"
    headless: bool = True
    slow_mo_ms: int = 0


class YouLearnCfg(BaseModel):
    dashboard_url: str = "https://app.youlearn.ai/"
    login_url: str = "https://app.youlearn.ai/signin"
    list_poll_interval_s: float = 2.0
    list_poll_timeout_s: float = 900.0
    post_upload_settle_s: float = 240.0
    post_upload_settle_poll_s: float = 15.0


class UploadsCfg(BaseModel):
    extensions: list[str] = Field(
        default_factory=lambda: [".txt", ".pdf", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"]
    )
    sort: Literal["name_asc", "name_desc", "mtime_asc", "mtime_desc"] = "name_asc"


class AuthCfg(BaseModel):
    email: str = ""
    password: str = ""
    google_client_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.email.strip() and self.password)


class Config(BaseModel):
    server: ServerCfg = ServerCfg()
    browser: BrowserCfg = BrowserCfg()
    youlearn: YouLearnCfg = YouLearnCfg()
    uploads: UploadsCfg = UploadsCfg()
    auth: AuthCfg = AuthCfg()

    @property
    def profile_path(self) -> Path:
        p = Path(self.browser.profile_dir)
        return p if p.is_absolute() else ROOT / p


def load() -> Config:
    cfg_file = ROOT / "config.yaml"
    data = {}
    if cfg_file.exists():
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}

    auth_data = dict(data.get("auth") or {})
    auth_data["email"] = os.getenv("YOULERN_EMAIL", auth_data.get("email", ""))
    auth_data["password"] = os.getenv("YOULERN_PASSWORD", auth_data.get("password", ""))
    auth_data["google_client_id"] = (
        os.getenv("VITE_GOOGLE_CLIENT_ID")
        or os.getenv("NEXT_PUBLIC_GOOGLE_CLIENT_ID")
        or os.getenv("GOOGLE_CLIENT_ID")
        or auth_data.get("google_client_id", "")
    )
    data["auth"] = auth_data

    return Config(**data)
