"""MiMo Desktop / 官方 API 客户端

双通道路由：
  Preview 独占模型 (mimo-x-pro-preview / mimo-x-flash-preview)
      → POST {MIMO_SERVER}/api/route/chat/completions
        Cookie: serviceToken=...   UA: miaccount_desktop
  稳定模型
      → 优先 api.xiaomimimo.com/v1/chat/completions（sk- key）
      → 无 key 时回落 Desktop /api/route

上游已是 OpenAI 兼容格式，本层只做鉴权、路由与错误映射。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional, Tuple

import httpx

from .config import MimoAccount
from .desktop_session import (
    API_BASE,
    API_UA,
    get_service_cookie,
    invalidate_session_cache,
)

PREVIEW_MODELS = {"mimo-x-pro-preview", "mimo-x-flash-preview"}
DEFAULT_OFFICIAL_BASE = "https://api.xiaomimimo.com/v1"
OFFICIAL_SOURCE_HEADER = {"X-Mimo-Source": "mimocode-cli"}

TIMEOUT = 180.0


class MimoApiError(Exception):
    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"MiMo API error {status_code}: {response_body[:200]}")


def bare_model(model: str) -> str:
    s = str(model or "")
    i = s.find("/")
    return s[i + 1:] if i >= 0 else s


def is_preview_model(model: str) -> bool:
    return bare_model(model) in PREVIEW_MODELS


def _preview_upstream_model(model: str) -> str:
    return f"xiaomi/{bare_model(model)}"


def _apply_preview_defaults(body: dict) -> dict:
    out = dict(body)
    out["model"] = _preview_upstream_model(out.get("model", ""))
    if out.get("thinking") is None:
        out["thinking"] = {"type": "enabled"}
    if out.get("temperature") is None:
        out["temperature"] = 1.0
    if out.get("top_p") is None:
        out["top_p"] = 0.95
    if out.get("max_tokens") is None:
        out["max_tokens"] = 4096
    return out


class MimoClient:
    """Desktop 会话 / 官方 API 客户端"""

    def __init__(self, account: MimoAccount):
        self.account = account

    def _credentials(self) -> dict:
        return {
            "mimoPassToken": self.account.mimo_pass_token or None,
            "mimoUserId": self.account.mimo_user_id or None,
            "mimoCUserId": self.account.mimo_c_user_id or None,
            "apiKey": self.account.api_key or None,
        }

    def _route(self, model: str) -> str:
        """返回 desktop | official"""
        if is_preview_model(model):
            return "desktop"
        if self.account.has_api_key():
            return "official"
        return "desktop"

    async def _desktop_headers(self, stream: bool, client: httpx.AsyncClient) -> dict:
        cookie = await get_service_cookie(self._credentials(), client)
        if not cookie:
            raise MimoApiError(
                401,
                "Xiaomi account session unavailable. "
                "Sign in to MiMo Desktop once, or import passToken.",
            )
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": API_UA,
            "Cookie": cookie,
        }

    def _official_headers(self, stream: bool) -> dict:
        key = (self.account.api_key or "").strip()
        if not key:
            raise MimoApiError(401, "No sk- API key configured for official route.")
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": f"Bearer {key}",
            **OFFICIAL_SOURCE_HEADER,
        }

    def _url_for(self, model: str, route: str) -> str:
        if route == "desktop":
            return f"{API_BASE}/api/route/chat/completions"
        base = (self.account.base_url or DEFAULT_OFFICIAL_BASE).rstrip("/")
        return f"{base}/chat/completions"

    def _prepare_body(self, model: str, body: dict, route: str) -> dict:
        out = dict(body)
        out["model"] = bare_model(model) if route == "official" else _preview_upstream_model(model)
        if route == "desktop" and is_preview_model(model):
            out = _apply_preview_defaults({**body, "model": _preview_upstream_model(model)})
        return out

    async def chat_completion(
        self,
        body: dict,
        stream: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> httpx.Response:
        """发起一次 chat completion，返回原始 httpx.Response（调用方处理 SSE/JSON）。"""
        model = body.get("model", "")
        route = self._route(model)
        url = self._url_for(model, route)
        payload = self._prepare_body(model, body, route)

        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            headers = (
                await self._desktop_headers(stream, client)
                if route == "desktop"
                else self._official_headers(stream)
            )
            req_kwargs = {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": payload,
            }
            if stream:
                # stream=True 需要调用方在 client 上下文里读；这里用非 client 关闭方式
                request = client.build_request(**req_kwargs)
                response = await client.send(request, stream=True)
            else:
                response = await client.request(**req_kwargs)

            if response.status_code == 401 and route == "desktop":
                invalidate_session_cache(self.account.mimo_pass_token or None)
                headers = await self._desktop_headers(stream, client)
                if stream:
                    request = client.build_request(
                        method="POST", url=url, headers=headers, json=payload
                    )
                    response = await client.send(request, stream=True)
                else:
                    response = await client.request(
                        method="POST", url=url, headers=headers, json=payload
                    )

            if response.status_code >= 400:
                err_body = response.text if not stream else (await response.aread()).decode("utf-8", "replace")
                if stream:
                    await response.aclose()
                raise MimoApiError(response.status_code, err_body)

            if stream:
                return response  # caller must aclose; client must stay alive
            return response
        finally:
            if owns and not stream:
                await client.aclose()

    async def chat_completion_json(self, body: dict) -> dict:
        """非流式：返回解析后的 OpenAI 兼容 JSON。"""
        # 使用长生命周期 client 包住，确保 401 重试路径一致
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await self.chat_completion(body, stream=False, client=client)
            return resp.json()

    async def chat_completion_stream(
        self, body: dict
    ) -> AsyncIterator[bytes]:
        """流式：yield 原始 SSE 字节块（data: ...）。调用方无需解析上游格式。"""
        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await client.aclose()

    async def list_models(self, client: Optional[httpx.AsyncClient] = None) -> list[str]:
        """从官方 API 拉模型列表；无 key 时返回内置 Desktop 列表。"""
        if self.account.has_api_key():
            owns = client is None
            if owns:
                client = httpx.AsyncClient(timeout=15.0)
            try:
                base = (self.account.base_url or DEFAULT_OFFICIAL_BASE).rstrip("/")
                res = await client.get(
                    f"{base}/models",
                    headers=self._official_headers(stream=False),
                )
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("data") or []
                    ids = [m.get("id") for m in items if m.get("id")]
                    # 合并 Desktop 独占模型
                    for p in sorted(PREVIEW_MODELS):
                        if p not in ids:
                            ids.append(p)
                    return ids
            except Exception:
                pass
            finally:
                if owns:
                    await client.aclose()

        return [
            "mimo-x-pro-preview",
            "mimo-x-flash-preview",
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "mimo-v2-omni",
            "mimo-v2-flash",
        ]

    async def test_connection(self) -> Tuple[bool, str]:
        """无副作用探活：打 /v1/models 或发最小 usage 查询。"""
        try:
            models = await self.list_models()
            if not models:
                return False, "no models"
            return True, f"{len(models)} models"
        except MimoApiError as e:
            return False, f"HTTP {e.status_code}: {e.response_body[:80]}"
        except Exception as e:
            return False, str(e)[:120]

    # ── 兼容旧 web-bot 接口（供 anthropic_routes 使用） ──────────
    # 旧: call_api(query) → (content, think_content, usage, citations)
    # 旧: stream_api(query) → yield {"type": "text"|"think"|"usage", ...}

    def _query_body(
        self,
        query: str,
        thinking: bool,
        model: str,
        multi_medias: list | None = None,
        attachments: list | None = None,
    ) -> dict:
        content: str | list = query
        # Desktop/官方 API 接受 OpenAI content parts
        parts: list = [{"type": "text", "text": query}]
        for m in multi_medias or []:
            if m.get("type") == "image_url" or m.get("url"):
                parts.append({"type": "image_url", "image_url": {"url": m.get("url") or m.get("image_url")}})
        if len(parts) > 1:
            content = parts
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        }
        if thinking:
            body["reasoning_effort"] = "high"
        return body

    @staticmethod
    def _split_think_from_content(content: str) -> tuple[str, str]:
        open_tag, close_tag = "<think>", "</think>"
        if open_tag not in content:
            return content, ""
        main_parts, think_parts = [], []
        in_think = False
        buf = ""
        i = 0
        while i < len(content):
            if not in_think and content.startswith(open_tag, i):
                in_think = True
                i += len(open_tag)
                continue
            if in_think and content.startswith(close_tag, i):
                in_think = False
                think_parts.append(buf)
                buf = ""
                i += len(close_tag)
                continue
            buf += content[i]
            i += 1
        if in_think:
            think_parts.append(buf)
        else:
            main_parts.append(buf)
        return "".join(main_parts), "\n".join(think_parts)

    async def call_api(
        self,
        query: str,
        thinking: bool = False,
        model: str = "mimo-v2.5-pro",
        multi_medias: list | None = None,
        attachments: list | None = None,
        conversation_id: str | None = None,
    ) -> Tuple[str, str, dict, list]:
        """兼容旧接口。返回 (content, think_content, usage, citations)。"""
        body = self._query_body(query, thinking, model, multi_medias, attachments)
        data = await self.chat_completion_json(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw = message.get("content") or ""
        content, think = self._split_think_from_content(raw)
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens") or usage.get("promptTokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("completionTokens") or 0
        return content, think, {
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
        }, []

    async def stream_api(
        self,
        query: str,
        thinking: bool = False,
        model: str = "mimo-v2.5-pro",
        multi_medias: list | None = None,
        attachments: list | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """兼容旧流式接口。yield OpenAI SSE 语义化事件。"""
        body = self._query_body(query, thinking, model, multi_medias, attachments)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            buffer = ""
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield {
                        "type": "usage",
                        "promptTokens": u.get("prompt_tokens") or 0,
                        "completionTokens": u.get("completion_tokens") or 0,
                    }

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        yield {"type": "text", "content": piece}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        yield {"type": "think", "content": reasoning}
        finally:
            await client.aclose()
