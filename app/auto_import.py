"""Desktop 凭证自动导入

来源优先级：
  1. mimocode auth.json 的 xiaomi sk- key（官方 API）
  2. Desktop cookie 库的 passToken（会话续期 / Preview 模型）
"""

from __future__ import annotations

from typing import Any, Optional

from .desktop_session import (
    auth_json_candidates,
    read_auth_json_api_key,
    read_desktop_pass_token,
)


async def auto_import_desktop() -> dict[str, Any]:
    """探测本机凭证。返回 {found, apiKey, mimoPassToken, ...}。"""
    api = read_auth_json_api_key()
    pt = read_desktop_pass_token()

    if not api and not pt:
        checked = "\n".join(str(p) for p in auth_json_candidates())
        return {
            "found": False,
            "error": (
                "MiMo Desktop credentials not found.\n"
                f"Checked auth.json:\n{checked}\n"
                "and Desktop cookie store. Sign in to MiMo Desktop once, then retry."
            ),
        }

    return {
        "found": True,
        "apiKey": api["apiKey"] if api else None,
        "uid": (api or {}).get("uid") or (pt or {}).get("userId") or None,
        "baseUrl": (api or {}).get("baseUrl"),
        "source": (api or {}).get("source") or "desktop-cookie",
        "mimoPassToken": (pt or {}).get("passToken"),
        "mimoUserId": (pt or {}).get("userId"),
        "mimoCUserId": (pt or {}).get("cUserId"),
    }


def apply_import_payload(data: dict) -> dict:
    """把 auto-import 结果整理成 MimoAccount 字段。"""
    return {
        "uid": data.get("uid") or "",
        "api_key": (data.get("apiKey") or "").strip(),
        "base_url": data.get("baseUrl") or "https://api.xiaomimimo.com/v1",
        "mimo_pass_token": data.get("mimoPassToken") or "",
        "mimo_user_id": data.get("mimoUserId") or "",
        "mimo_c_user_id": data.get("mimoCUserId") or "",
    }
