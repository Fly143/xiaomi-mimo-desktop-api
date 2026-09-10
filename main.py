"""Xiaomi MiMo Desktop API — 主入口

MiMo Desktop 会话 / 官方 API → OpenAI + Anthropic 兼容。
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.anthropic_routes import router as anthropic_router
from app.batch import init_batch_storage
from app.config import config_manager
from app.routes import router

app = FastAPI(
    title="Xiaomi MiMo Desktop API",
    description="MiMo Desktop session → OpenAI + Anthropic API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(anthropic_router)

_batch_dir = Path(__file__).parent / ".anthropic_batches"
init_batch_storage(str(_batch_dir))

web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.on_event("startup")
async def on_startup():
    n = len(config_manager.config.mimo_accounts)
    print(f"[Startup] accounts={n}  api_keys={len(config_manager.config.api_keys.split(','))}")


def main():
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "127.0.0.1")
    print(f"""
Xiaomi MiMo Desktop API
  http://{host}:{port}
  OpenAI:    /v1/chat/completions
  Anthropic: /v1/messages
  Admin:     /   (HTTP Basic)
""")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
