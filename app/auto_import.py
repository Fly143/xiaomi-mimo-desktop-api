"""Desktop 凭证自动导入

只读本机 MiMo Desktop cookie 库的 passToken。
不读 mimocode auth.json 的 sk- key（那是官方 API，本项目不代理）。
"""

from __future__ import annotations

from typing import Any

from .desktop_session import desktop_cookie_path, read_desktop_pass_token


async def auto_import_desktop() -> dict[str, Any]:
    pt = read_desktop_pass_token()
    if not pt:
        path = desktop_cookie_path()
        where = str(path) if path else "Desktop cookie store not found"
        return {
            "found": False,
            "error": (
                "MiMo Desktop passToken not found.\n"
                f"Cookie DB: {where}\n"
                "Sign in to MiMo Desktop once, quit Desktop (it locks the cookie DB), then retry."
            ),
        }

    return {
        "found": True,
        "source": "desktop-cookie",
        "mimoPassToken": pt["passToken"],
        "mimoUserId": pt.get("userId"),
        "mimoCUserId": pt.get("cUserId"),
        "uid": pt.get("userId"),
    }


def apply_import_payload(data: dict) -> dict:
    return {
        "uid": data.get("uid") or data.get("mimoUserId") or "",
        "mimo_pass_token": data.get("mimoPassToken") or "",
        "mimo_user_id": data.get("mimoUserId") or "",
        "mimo_c_user_id": data.get("mimoCUserId") or "",
    }
