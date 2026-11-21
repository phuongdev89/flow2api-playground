"""FastAPI app của Playground — project độc lập, riêng container.

Playground là front-end generation đứng trước flow2api: gọi flow2api qua HTTP
(/v1/chat/completions), tự hậu xử lý kết quả (xoá watermark) và lưu media vào
tmp riêng của mình.
"""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import generation, watermark
from .config import DATA_DIR, TMP_DIR
from .routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Flow Playground", docs_url=None, redoc_url=None, openapi_url=None)

# Media nội bộ (refs + kết quả đã hậu xử lý) — mount static như flow2api cũ.
TMP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
app.mount("/tmp", StaticFiles(directory=str(TMP_DIR)), name="tmp")

app.include_router(router)


@app.get("/health", include_in_schema=False)
async def health():
    return {
        "status": "ok",
        "service": "playground",
        "flow2api": generation.flow_client.base_url,
        "watermark": {
            "enabled": watermark.WATERMARK_ENABLED,
            "engine": watermark.get_engine_name(),
        },
    }


@app.on_event("startup")
async def _startup_log():
    mask_count = len(watermark.list_saved_masks())
    logging.getLogger("playground").info(
        "Playground started — flow2api=%s watermark engine=%s masks=%d",
        generation.flow_client.base_url,
        watermark.get_engine_name(),
        mask_count,
    )
