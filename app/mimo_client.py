"""MiMo Desktop 会话客户端

唯一上游：mimo-server /api/route/chat/completions
鉴权：passToken → SSO → serviceToken cookie（见 desktop_session.py）

不代理 api.xiaomimimo.com 官方 OpenAI API。
上游返回 OpenAI 兼容 JSON/SSE，本层只做鉴权、模型名映射、错误与 401 重试。
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional, Tuple

import httpx

from .config import MimoAccount
from .desktop_session import (
    API_BASE,
    API_UA,
    get_service_cookie,
    invalidate_session_cache,
)

# Desktop 独占模型需要 xiaomi/ 前缀
PREVIEW_MODELS = {"mimo-x-pro-preview", "mimo-x-flash-preview"}
TIMEOUT = 180.0
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

BUILTIN_MODELS = [
    "mimo-x-pro-preview",
    "mimo-x-flash-preview",
]


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


def upstream_model_name(model: str) -> str:
    """Desktop /api/route 期望的模型名。"""
    if is_preview_model(model):
        return f"xiaomi/{bare_model(model)}"
    return bare_model(model)


class MimoClient:
    """Desktop 账号会话客户端"""

    def __init__(self, account: MimoAccount):
        self.account = account

    def _credentials(self) -> dict:
        return {
            "mimoPassToken": self.account.mimo_pass_token or None,
            "mimoUserId": self.account.mimo_user_id or None,
            "mimoCUserId": self.account.mimo_c_user_id or None,
        }

    async def _headers(self, stream: bool, client: httpx.AsyncClient) -> dict:
        cookie = await get_service_cookie(self._credentials(), client)
        if not cookie:
            raise MimoApiError(
                401,
                "Xiaomi Desktop session unavailable. "
                "Sign in to MiMo Desktop once so passToken is present, then retry.",
            )
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": API_UA,
            "Cookie": cookie,
        }

    def _url(self) -> str:
        return f"{API_BASE}/api/route/chat/completions"

    def _prepare_body(self, body: dict) -> dict:
        out = dict(body)
        model = out.get("model", "")
        out["model"] = upstream_model_name(model)
        if is_preview_model(model):
            if out.get("thinking") is None:
                out["thinking"] = {"type": "enabled"}
            if out.get("temperature") is None:
                out["temperature"] = 1.0
            if out.get("top_p") is None:
                out["top_p"] = 0.95
            # 不猜 max_tokens：上游语义未知（可能是 reasoning + 正文的合计预算），
            # 代理层填默认值容易让思考链吃光预算导致正文为空。未显式指定时透传。
        return out

    async def chat_completion(
        self,
        body: dict,
        stream: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> httpx.Response:
        url = self._url()
        payload = self._prepare_body(body)

        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            headers = await self._headers(stream, client)

            async def _send(hdrs: dict) -> httpx.Response:
                if stream:
                    req = client.build_request("POST", url, headers=hdrs, json=payload)
                    return await client.send(req, stream=True)
                return await client.request("POST", url, headers=hdrs, json=payload)

            response = await _send(headers)

            if response.status_code == 401:
                invalidate_session_cache(self.account.mimo_pass_token or None)
                headers = await self._headers(stream, client)
                if stream:
                    await response.aclose()
                response = await _send(headers)

            if response.status_code >= 400:
                err = (
                    response.text
                    if not stream
                    else (await response.aread()).decode("utf-8", "replace")
                )
                if stream:
                    await response.aclose()
                raise MimoApiError(response.status_code, err)

            return response
        except Exception:
            if owns and not stream:
                await client.aclose()
            raise
        finally:
            if owns and not stream:
                await client.aclose()

    async def chat_completion_json(self, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await self.chat_completion(body, stream=False, client=client)
            return resp.json()

    async def chat_completion_stream(self, body: dict) -> AsyncIterator[bytes]:
        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await client.aclose()

    async def list_models(self) -> list[str]:
        """Desktop 会话没有公开 models 列表接口时返回内置列表。"""
        return list(BUILTIN_MODELS)

    async def test_connection(self) -> Tuple[bool, str]:
        """探活：打一次最小流式请求验证 Desktop 会话。
        按用户\"全部移除限制\"指令：不填 max_tokens，让上游/模型自行决定输出长度。"""
        if not self.account.has_session():
            return False, "no passToken configured"
        try:
            data = await self.chat_completion_json(
                {
                    "model": "mimo-x-flash-preview",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            if data.get("error"):
                return False, str(data["error"])[:120]
            return True, "session ok"
        except MimoApiError as e:
            return False, f"HTTP {e.status_code}: {e.response_body[:80]}"
        except Exception as e:
            return False, str(e)[:120]

    # ── 兼容 anthropic_routes / routes 的旧接口 ──────────────────

    @staticmethod
    def _normalize_tools(tools: list | None) -> list | None:
        """规范化为 Chat Completions 的 {type, function:{name,...}} 格式。

        Responses API 的 tools 是扁平的（name/parameters 在顶层），
        直接透传会让上游报 `function' is null`。
        非 function 类型（web_search / computer_use 等）丢弃。
        """
        if not tools:
            return None
        out = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function")
            # Chat Completions 标准格式
            if isinstance(fn, dict) and fn.get("name"):
                out.append({
                    "type": "function",
                    "function": {
                        "name": fn["name"],
                        "description": fn.get("description") or "",
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    },
                })
                continue
            # Responses API 扁平格式：{type:function, name, parameters, description}
            if t.get("name") and (t.get("type") in (None, "function")):
                out.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description") or "",
                        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                    },
                })
        return out or None

    def _query_body(
        self,
        query: str,
        thinking: bool,
        model: str,
        multi_medias: list | None = None,
        attachments: list | None = None,
        tools: list | None = None,
        stream: bool = False,
    ) -> dict:
        parts: list = [{"type": "text", "text": query}]
        for m in multi_medias or []:
            url = m.get("url") or m.get("image_url")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        content = parts if len(parts) > 1 else query
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": stream,
        }
        if thinking:
            body["reasoning_effort"] = "high"
        # Desktop OpenAI 兼容：传原生 tools，优先返回结构化 tool_calls
        norm_tools = self._normalize_tools(tools)
        if norm_tools:
            body["tools"] = norm_tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _split_think_from_content(content: str) -> tuple[str, str]:
        open_tag, close_tag = "<think>", "</think>"
        if open_tag not in content:
            return content, ""
        main, think, in_think, buf, i = [], [], False, "", 0
        while i < len(content):
            if not in_think and content.startswith(open_tag, i):
                in_think = True
                i += len(open_tag)
                continue
            if in_think and content.startswith(close_tag, i):
                in_think = False
                think.append(buf)
                buf = ""
                i += len(close_tag)
                continue
            buf += content[i]
            i += 1
        if in_think:
            think.append(buf)
        else:
            main.append(buf)
        return "".join(main), "\n".join(think)

    @staticmethod
    def _native_tool_calls_to_text(tool_calls: list) -> str:
        """OpenAI message.tool_calls → 文本，供 extract_tool_call / StreamSieve 解析。"""
        lines = []
        for tc in tool_calls or []:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or "{}"
            if not name:
                continue
            lines.append(f"TOOL_CALL: {name}({args})")
        return "\n".join(lines)

    @staticmethod
    def _merge_stream_tool_calls(acc: dict, deltas: list) -> list:
        """合并 SSE 里分片的 delta.tool_calls，按 index 聚合。"""
        for d in deltas or []:
            idx = d.get("index", 0)
            slot = acc.setdefault(idx, {"id": None, "function": {"name": "", "arguments": ""}})
            if d.get("id"):
                slot["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
        return [acc[i] for i in sorted(acc)]

    async def call_api(
        self, query: str, thinking: bool = False, model: str = "mimo-x-pro-preview",
        multi_medias: list | None = None, attachments: list | None = None,
        conversation_id: str | None = None, tools: list | None = None,
    ) -> Tuple[str, str, dict, list]:
        body = self._query_body(query, thinking, model, multi_medias, attachments, tools=tools)
        data = await self.chat_completion_json(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw = message.get("content") or ""
        content, think = self._split_think_from_content(raw)

        # OpenAI 原生 reasoning
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if reasoning:
            think = (think + "\n" if think else "") + reasoning

        # OpenAI 原生 tool_calls → 文本，走既有 extract_tool_call
        native_tc = message.get("tool_calls") or []
        if native_tc:
            tc_text = self._native_tool_calls_to_text(native_tc)
            content = (content + "\n" if content else "") + tc_text

        usage = data.get("usage") or {}
        return content, think, {
            "promptTokens": usage.get("prompt_tokens") or 0,
            "completionTokens": usage.get("completion_tokens") or 0,
        }, []

    async def stream_api(
        self, query: str, thinking: bool = False, model: str = "mimo-x-pro-preview",
        multi_medias: list | None = None, attachments: list | None = None,
        conversation_id: str | None = None, tools: list | None = None,
    ) -> AsyncIterator[dict]:
        body = self._query_body(query, thinking, model, multi_medias, attachments, tools=tools, stream=True)
        body["stream_options"] = {"include_usage": True}
        client = httpx.AsyncClient(timeout=TIMEOUT)
        tc_acc: dict = {}
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    # 收尾：完整 tool_calls 以文本交给 sieve / extract_tool_call
                    merged = self._merge_stream_tool_calls(tc_acc, None)
                    if merged:
                        text = self._native_tool_calls_to_text(merged)
                        if text:
                            yield {"type": "text", "content": "\n" + text + "\n"}
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
                    if delta.get("content"):
                        yield {"type": "text", "content": delta["content"]}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        # 包成 think 块，routes 既有 THINK_OPEN/CLOSE 逻辑可直接处理
                        yield {"type": "text", "content": THINK_OPEN + reasoning + THINK_CLOSE}
                    if delta.get("tool_calls"):
                        self._merge_stream_tool_calls(tc_acc, delta["tool_calls"])
        finally:
            await client.aclose()
