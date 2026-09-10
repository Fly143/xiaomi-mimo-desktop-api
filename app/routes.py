"""API 路由 — MiMo2API Desktop

OpenAI /v1/* 代理 + 模型发现 + 管理后台 + Desktop 凭证导入。
上游是 Desktop 账号会话的 /api/route/chat/completions（OpenAI 兼容）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime as _dt
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import verify_admin
from .auto_import import apply_import_payload, auto_import_desktop
from .config import MimoAccount, config_manager
from .desktop_session import get_account_usage
from .mimo_client import MimoApiError, MimoClient, is_preview_model
from .models import OpenAIRequest
from .usage_store import add_usage as _add_usage
from .usage_store import clear_usage as _clear_usage
from .usage_store import get_usage as _get_usage

router = APIRouter()

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# 兼容 anthropic_routes 的导入
def validate_api_key(authorization: Optional[str]) -> bool:
    if not authorization:
        return False
    key = authorization.replace("Bearer ", "", 1).strip()
    return config_manager.validate_api_key(key)


def _strip_tool_result_blocks(text: str) -> str:
    if not text:
        return text
    cleaned = re.sub(r"\[TOOL_RESULT\]\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[/TOOL_RESULT\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[tool_result\s+id=\S+\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?tool_result>\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _camel_case(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _strip_tool_name_prefix(text: str, tool_names: list) -> str:
    if not text or not tool_names:
        return text
    variants = []
    for n in tool_names:
        variants.append(re.escape(n))
        if "_" in n:
            variants.append(re.escape(_camel_case(n)))
    cleaned = re.sub(
        rf"^({'|'.join(variants)})\s*\n?", "", text.strip(), flags=re.IGNORECASE
    )
    return cleaned.strip()


def _strip_mimo_prefix(text: str) -> str:
    if not text:
        return text
    prefixes = [
        "webSearch", "getTimeInfo", "getTime", "sessionSearch",
        "imageSearch", "fileSearch", "getLocation", "webExtract",
        "getWeather", "calculator",
    ]
    escaped = "|".join(re.escape(p) for p in prefixes)
    return re.sub(rf"^({escaped})\s*\n?", "", text.strip(), flags=re.IGNORECASE).strip()


def _safe_flush(text: str) -> Tuple[str, str]:
    last_lt = text.rfind("<")
    if last_lt == -1:
        return text, ""
    suffix = text[last_lt:]
    if THINK_OPEN.startswith(suffix) or THINK_CLOSE.startswith(suffix):
        return text[:last_lt], suffix
    return text, ""


# ─── 模型上下文 ─────────────────────────────────────────────

def _model_context(model_id: str) -> Optional[dict]:
    m = model_id.lower()
    if "preview" in m:
        return {"context_length": 262144, "max_output_tokens": 32768}
    if any(x in m for x in ("v2.5-pro", "v2-pro", "v2.5")):
        return {"context_length": 1048576, "max_output_tokens": 131072}
    if "v2-flash" in m:
        return {"context_length": 262144, "max_output_tokens": 65536}
    if "v2-omni" in m:
        return {"context_length": 262144, "max_output_tokens": 131072}
    return None


_models_cache: Optional[list[str]] = None
_models_lock = asyncio.Lock()


def _pick_account() -> MimoAccount:
    acc = config_manager.get_next_account()
    if not acc:
        raise HTTPException(
            503,
            detail={"error": {"message": "No Xiaomi account configured. Import Desktop credentials first."}},
        )
    return acc


async def _resolve_models() -> list[str]:
    global _models_cache
    async with _models_lock:
        if config_manager.config.models:
            return list(config_manager.config.models)
        acc = config_manager.get_next_account()
        if not acc:
            return [
                "mimo-x-pro-preview",
                "mimo-x-flash-preview",
                "mimo-v2.5-pro",
                "mimo-v2.5",
                "mimo-v2-omni",
                "mimo-v2-flash",
            ]
        try:
            ids = await MimoClient(acc).list_models()
            if ids:
                _models_cache = ids
                return ids
        except Exception:
            pass
        return _models_cache or [
            "mimo-x-pro-preview",
            "mimo-x-flash-preview",
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "mimo-v2-omni",
            "mimo-v2-flash",
        ]


@router.get("/v1/models")
async def list_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(401, detail={"error": {"message": "invalid api key"}})
    models = await _resolve_models()
    data = []
    for m in models:
        obj = {"id": m, "object": "model", "created": 1735689600, "owned_by": "xiaomi"}
        ctx = _model_context(m)
        if ctx:
            obj.update({
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            })
        data.append(obj)
    return {"object": "list", "data": data}


@router.post("/v1/models/refresh")
async def refresh_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(401, detail={"error": {"message": "invalid api key"}})
    global _models_cache
    _models_cache = None
    models = await _resolve_models()
    return {"object": "list", "count": len(models), "data": models}


# ─── OpenAI Chat Completions ────────────────────────────────

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    model = body.get("model") or "mimo-v2.5-pro"
    stream = bool(body.get("stream"))
    account = _pick_account()
    client = MimoClient(account)

    # 上游 OpenAI 兼容：透传 body，仅补 stream 相关字段
    upstream = dict(body)
    upstream["model"] = model
    if stream:
        upstream["stream"] = True
        upstream.setdefault("stream_options", {"include_usage": True})

    try:
        if not stream:
            data = await client.chat_completion_json(upstream)
            usage = data.get("usage") or {}
            try:
                _add_usage(
                    model,
                    usage.get("prompt_tokens") or 0,
                    usage.get("completion_tokens") or 0,
                )
            except Exception:
                pass
            return JSONResponse(data)

        async def event_gen():
            async for chunk in client.chat_completion_stream(upstream):
                yield chunk

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except MimoApiError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.response_body[:500], "type": "upstream_error"}},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e)[:300], "type": "internal_error"}},
        )


# ─── Desktop 自动导入 ───────────────────────────────────────

@router.get("/api/desktop/auto-import")
async def desktop_auto_import(username: str = Depends(verify_admin)):
    return await auto_import_desktop()


@router.post("/api/desktop/import")
async def desktop_import(request: Request, username: str = Depends(verify_admin)):
    data = await request.json()
    fields = apply_import_payload(data)
    if not fields["mimo_pass_token"]:
        raise HTTPException(400, "Need mimoPassToken from Desktop cookie store")

    now = _dt.now().strftime("%m-%d %H:%M")
    uid = fields.get("uid") or fields.get("mimo_user_id") or ""
    existing = False
    for i, acc in enumerate(config_manager.config.mimo_accounts):
        if uid and acc.uid == uid:
            config_manager.config.mimo_accounts[i] = MimoAccount(
                mimo_pass_token=fields["mimo_pass_token"] or acc.mimo_pass_token,
                mimo_user_id=fields["mimo_user_id"] or acc.mimo_user_id,
                mimo_c_user_id=fields["mimo_c_user_id"] or acc.mimo_c_user_id,
                uid=uid or acc.uid,
                login_time=now,
                is_valid=True,
            )
            existing = True
            break
    if not existing:
        config_manager.config.mimo_accounts.append(
            MimoAccount(
                mimo_pass_token=fields["mimo_pass_token"],
                mimo_user_id=fields["mimo_user_id"],
                mimo_c_user_id=fields["mimo_c_user_id"],
                uid=uid,
                login_time=now,
                is_valid=True,
            )
        )
    config_manager.save()

    acc = config_manager.config.mimo_accounts[-1] if not existing else next(
        a for a in config_manager.config.mimo_accounts if a.uid == uid
    )
    ok, msg = await MimoClient(acc).test_connection()
    return {"ok": True, "validated": ok, "detail": msg, "uid": uid}


# ─── 账号管理 ───────────────────────────────────────────────

@router.get("/api/accounts")
async def list_accounts(username: str = Depends(verify_admin)):
    out = []
    for acc in config_manager.config.mimo_accounts:
        d = acc.to_masked_dict()
        d["has_session"] = acc.has_session()
        out.append(d)
    return {"accounts": out}


@router.delete("/api/accounts/{idx}")
async def delete_account(idx: int, username: str = Depends(verify_admin)):
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    removed = accounts.pop(idx)
    config_manager.save()
    return {"ok": True, "uid": removed.uid}


@router.post("/api/accounts/{idx}/test")
async def test_account(idx: int, username: str = Depends(verify_admin)):
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    acc = accounts[idx]
    ok, msg = await MimoClient(acc).test_connection()
    acc.is_valid = ok
    acc.last_test = _dt.now().strftime("%m-%d %H:%M")
    config_manager.save()
    return {"ok": ok, "detail": msg}


@router.get("/api/accounts/{idx}/usage")
async def account_usage(idx: int, username: str = Depends(verify_admin)):
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    acc = accounts[idx]
    creds = {
        "mimoPassToken": acc.mimo_pass_token or None,
        "mimoUserId": acc.mimo_user_id or None,
        "mimoCUserId": acc.mimo_c_user_id or None,
    }
    info = await get_account_usage(creds)
    if "error" in info:
        return {"ok": False, "error": info["error"]}
    percent = info.get("percent")
    remaining = max(0, min(100, round(percent))) if isinstance(percent, (int, float)) else None
    return {
        "ok": True,
        "plan": "Xiaomi MiMo Desktop",
        "percent_remaining": remaining,
        "used_percent": (100 - remaining) if remaining is not None else None,
        "resetDate": info.get("resetDate"),
        "resetAt": info.get("resetAt"),
    }


# ─── 配置 / 使用量 / 管理页 ────────────────────────────────

@router.get("/api/config")
async def get_config(username: str = Depends(verify_admin)):
    return config_manager.get_config()


@router.post("/api/config")
async def update_config(request: Request, username: str = Depends(verify_admin)):
    data = await request.json()
    config_manager.update_config(data)
    return {"ok": True}


@router.get("/api/usage")
async def usage_stats(username: str = Depends(verify_admin)):
    return {"usage": _get_usage()}


@router.post("/api/usage/clear")
async def clear_usage(username: str = Depends(verify_admin)):
    _clear_usage()
    return {"ok": True}


@router.get("/")
async def index():
    from pathlib import Path
    from fastapi.responses import FileResponse
    p = Path(__file__).resolve().parent.parent / "web" / "index.html"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"name": "MiMo2API-Desktop", "docs": "/docs"})


@router.get("/admin")
async def admin_page():
    return await index()
