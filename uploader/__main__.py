from __future__ import annotations
import logging
from pathlib import Path
import uvicorn

from .config import load, ROOT

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "uploader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def main() -> None:
    cfg = load()
    uvicorn.run(
        "uploader.server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
