"""Xiaomi MiMo Desktop 账号会话

从本机 MiMo Desktop 的 cookie 库读取 passToken，复刻 Desktop 的 SSO 链路
换取 mimo-server serviceToken，用于：
  1. /api/route/chat/completions  — Desktop 独占模型（OpenAI 兼容）
  2. /api/user/usage              — 周配额

链路（对照 MiMo Desktop 流量）：
  GET  {api}/api/user/xiaomi/me                              → 302 拿 sts callback
  GET  account /pass/serviceLogin?sid=passportapi&_json=true → nonce/ssecurity
  GET  {location}&clientSign=...                             → 账号级 serviceToken
  GET  account /pass/serviceLogin?sid=mimopc&callback=<sts>  → ticket
  GET  {api}/api/sts?...&ticket...                           → Set-Cookie: serviceToken
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

API_BASE = "https://mimo-server-cn.xiaomimimo.com"
ACCOUNT_HOST = "account.xiaomi.com"
API_UA = (
    "miNative PC/Normal Windows_NT/10.0.19045 SDKV/1.0.0 "
    "DEVT/PC DEVS/Windows APP/miaccount_desktop APPV/0.1.0"
)
SSO_UA = "MiClaw/1.0"
COOKIE_TTL_S = 30 * 60  # 30 min

# key = sha256(passToken) → {cookie, at}
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}
_inflight_result: dict[str, Optional[str]] = {}


def desktop_cookie_path() -> Optional[Path]:
    home = Path.home()
    if os.name == "nt":
        p = home / "AppData/Roaming/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies"
    elif sys_platform_is_darwin():
        p = home / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies"
    else:
        p = home / ".config/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies"
    return p if p.exists() else None


def sys_platform_is_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


def auth_json_candidates() -> list[Path]:
    home = Path.home()
    paths = [home / ".local/share/mimocode/auth.json"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(home / "AppData/Roaming")
        paths.append(Path(appdata) / "Xiaomi MiMo/auth.json")
    else:
        paths.append(home / "Library/Application Support/mimocode/auth.json")
    return paths


def read_desktop_pass_token() -> Optional[dict]:
    """从 Desktop 的 Chromium cookie 库读取 passToken。

    Desktop 运行时会独占锁 cookie DB，copy 失败则返回 None。
    """
    src = desktop_cookie_path()
    if not src:
        return None

    fd, tmp = tempfile.mkstemp(prefix="m2a-cookies-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        try:
            shutil.copy2(src, tmp_path)
        except OSError:
            return None  # locked by running Desktop

        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, value FROM cookies WHERE host_key = ?",
                ("." + ACCOUNT_HOST,),
            ).fetchall()
        finally:
            conn.close()

        jar = {name: value for name, value in rows}
        if not jar.get("passToken"):
            return None
        return {
            "passToken": jar["passToken"],
            "userId": jar.get("userId") or None,
            "cUserId": jar.get("cUserId") or None,
        }
    except Exception:
        return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_auth_json_api_key() -> Optional[dict]:
    """读取 mimocode auth.json 里的 xiaomi sk- key（官方 API 通路）。"""
    for p in auth_json_candidates():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        xiaomi = (data or {}).get("xiaomi") or {}
        key = (xiaomi.get("key") or "").strip()
        if not key.startswith("sk-"):
            continue
        meta = xiaomi.get("metadata") or {}
        return {
            "apiKey": key,
            "uid": meta.get("uid") or None,
            "baseUrl": meta.get("base_url") or "https://api.xiaomimimo.com/v1",
            "source": str(p),
        }
    return None


def _client_sign(nonce: str, ssecurity: str | None) -> str:
    payload = f"nonce={nonce}"
    if ssecurity and ssecurity.strip():
        payload += f"&{ssecurity}"
    digest = hashlib.sha1(payload.encode()).digest()
    import base64
    return base64.b64encode(digest).decode().replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")


def _absorb_set_cookie(jar: dict, res: httpx.Response) -> None:
    for c in res.headers.get_list("set-cookie"):
        name_value = c.split(";", 1)[0]
        if "=" not in name_value:
            continue
        k, v = name_value.split("=", 1)
        if v:
            jar[k.strip()] = v.strip()


def _cookie_header(jar: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


async def _acquire_service_cookie(
    pass_jar: dict, client: httpx.AsyncClient
) -> Optional[str]:
    jar = dict(pass_jar)
    ck = lambda: _cookie_header(jar)  # noqa: E731

    # 1. 未认证 API → 302 携带 sts callback
    r1 = await client.get(
        f"{API_BASE}/api/user/xiaomi/me",
        headers={"User-Agent": API_UA, "Cookie": ck()},
        follow_redirects=False,
    )
    location = r1.headers.get("location")
    if not location:
        return None
    sts_callback = parse_qs(urlparse(location).query).get("callback", [None])[0]
    if not sts_callback:
        return None

    # 2. passportapi SSO phase 1 → nonce + ssecurity
    sso1 = await client.get(
        f"https://{ACCOUNT_HOST}/pass/serviceLogin",
        params={"sid": "passportapi", "_json": "true"},
        headers={"Cookie": ck(), "User-Agent": SSO_UA, "Accept": "application/json"},
    )
    text1 = sso1.text
    if text1.startswith("&&&START&&&"):
        text1 = text1[len("&&&START&&&"):]
    try:
        j1 = json.loads(text1)
    except json.JSONDecodeError:
        return None

    location1 = j1.get("location")
    if not location1:
        return None
    nonce = j1.get("nonce")
    if not nonce:
        qs = parse_qs(urlparse(location1).query)
        nonce = (qs.get("nonce") or [None])[0]
    if not nonce:
        return None

    # 3. passportapi SSO phase 2 → 账号级 serviceToken
    sep = "&" if "?" in location1 else "?"
    sso2 = await client.get(
        f"{location1}{sep}clientSign={_client_sign(nonce, j1.get('ssecurity'))}",
        headers={"Cookie": ck(), "User-Agent": SSO_UA},
        follow_redirects=False,
    )
    _absorb_set_cookie(jar, sso2)

    # 4. mimopc SSO → sts callback ticket
    sso3 = await client.get(
        f"https://{ACCOUNT_HOST}/pass/serviceLogin",
        params={
            "sid": "mimopc",
            "callback": sts_callback,
            "_json": "true",
        },
        headers={"Cookie": ck(), "User-Agent": SSO_UA, "Accept": "application/json"},
    )
    text3 = sso3.text
    if text3.startswith("&&&START&&&"):
        text3 = text3[len("&&&START&&&"):]
    try:
        j3 = json.loads(text3)
    except json.JSONDecodeError:
        return None
    _absorb_set_cookie(jar, sso3)

    loc3 = j3.get("location") or ""
    if "/api/sts" not in loc3:
        return None

    # 5. sts callback → Set-Cookie: serviceToken
    sts = await client.get(
        loc3,
        headers={"User-Agent": API_UA, "Cookie": ck()},
        follow_redirects=False,
    )
    _absorb_set_cookie(jar, sts)

    if not jar.get("serviceToken"):
        return None

    needed = ["serviceToken", "mimopc_ph", "mimopc_slh", "userId"]
    out = {k: jar[k] for k in needed if jar.get(k)}
    return _cookie_header(out)


async def get_service_cookie(
    credentials: dict, client: Optional[httpx.AsyncClient] = None
) -> Optional[str]:
    """获取（并缓存）mimo-server 账号会话 cookie。

    credentials 优先级：
      1. mimoPassToken（导入时落库的 per-account passToken）
      2. 本机 Desktop cookie 库
    """
    pass_jar: Optional[dict] = None
    if credentials.get("mimoPassToken"):
        pass_jar = {
            "passToken": credentials["mimoPassToken"],
            "userId": credentials.get("mimoUserId"),
            "cUserId": credentials.get("mimoCUserId"),
        }
    else:
        pass_jar = read_desktop_pass_token()

    if not pass_jar or not pass_jar.get("passToken"):
        return None

    key = hashlib.sha256(pass_jar["passToken"].encode()).hexdigest()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit["at"] < COOKIE_TTL_S:
            return hit["cookie"]
        if key in _inflight:
            ev = _inflight[key]
        else:
            ev = None

    if ev is not None:
        ev.wait(timeout=60)
        return _inflight_result.get(key)

    done = threading.Event()
    with _cache_lock:
        _inflight[key] = done

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        cookie = await _acquire_service_cookie(pass_jar, client)
    except Exception:
        cookie = None
    finally:
        if owns_client:
            await client.aclose()
        with _cache_lock:
            _inflight.pop(key, None)
            _inflight_result[key] = cookie

    if cookie:
        with _cache_lock:
            _cache[key] = {"cookie": cookie, "at": time.time()}
    done.set()
    return cookie


def invalidate_session_cache(pass_token: Optional[str] = None) -> None:
    with _cache_lock:
        if pass_token is None:
            _cache.clear()
            return
        key = hashlib.sha256(pass_token.encode()).hexdigest()
        _cache.pop(key, None)


async def get_account_usage(
    credentials: dict, client: Optional[httpx.AsyncClient] = None
) -> dict:
    """周配额。成功返回 {percent, resetDate, resetAt}，失败 {error}。"""
    cookie = await get_service_cookie(credentials, client)
    if not cookie:
        return {"error": "no-session"}

    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=15.0)
    try:
        res = await client.get(
            f"{API_BASE}/api/user/usage",
            headers={
                "User-Agent": API_UA,
                "Cookie": cookie,
                "Accept": "application/json",
            },
        )
        if res.status_code == 401:
            invalidate_session_cache(credentials.get("mimoPassToken"))
            return {"error": "http-401"}
        if res.status_code != 200:
            return {"error": f"http-{res.status_code}"}
        data = res.json()
        if not data or data.get("code") != 0 or not data.get("data"):
            return {"error": "bad-response"}
        d = data["data"]
        return {
            "percent": d.get("percent"),
            "resetDate": d.get("resetDate"),
            "resetAt": d.get("resetAt"),
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        if owns:
            await client.aclose()
