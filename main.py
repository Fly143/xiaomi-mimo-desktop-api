"""Xiaomi MiMo Desktop API 鈥?涓诲叆鍙?

灏嗗皬绫?MiMo Desktop 璐﹀彿浼氳瘽杞崲涓?OpenAI + Anthropic 鍏煎 API銆?
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.routes import router, _do_discover
from app.config import config_manager
from app.anthropic_routes import router as anthropic_router
from app.batch import init_batch_storage as init_anthropic_batches

app = FastAPI(
    title="Xiaomi MiMo Desktop API",
    description="MiMo Desktop session 鈫?OpenAI + Anthropic API (Chat / Responses / Anthropic Messages)",
    version="1.1.2",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_discover_models():
    init_anthropic_batches(str(Path(__file__).parent / ".anthropic_batches"))
    try:
        await _do_discover()
        print("妯″瀷棰勬帰娴嬪畬鎴?)
    except Exception as e:
        print(f"妯″瀷棰勬帰娴嬪け璐ワ紙涓嶅奖鍝嶆湇鍔★級: {e}")



app.include_router(router)
app.include_router(anthropic_router)

init_anthropic_batches(str(Path(__file__).parent / ".anthropic_batches"))

web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


def main():
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"""
Xiaomi MiMo Desktop API
  鍦板潃: http://{host}:{port}
  绠＄悊: http://{host}:{port}
  API:  http://{host}:{port}/v1/chat/completions
  鏂囨。: http://{host}:{port}/docs

  API Keys: {len(config_manager.config.api_keys.split(','))} 涓?
  Desktop 璐﹀彿: {len(config_manager.config.mimo_accounts)} 涓?
  妯″瀷: mimo-x-pro-preview / mimo-x-flash-preview
""")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
