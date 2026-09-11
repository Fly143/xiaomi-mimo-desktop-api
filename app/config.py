"""配置管理 — Desktop 版

账号凭证：mimo_pass_token / mimo_user_id / mimo_c_user_id
敏感字段落盘 Fernet 加密（enc:v1:），密钥在同目录 .secret_key。
旧明文配置可加载，下次 save 自动迁移。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_API_KEYS = "sk-mimo"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_TOOLS_PASSTHROUGH = True  # Desktop 原生 tools/tool_calls，跳过 MiMoML 说明书
DEFAULT_COMPRESSION_MODE = "compress"

ENC_PREFIX = "enc:v1:"
_SENSITIVE_ACCOUNT_FIELDS = (
    "mimo_pass_token",
    "mimo_user_id",
    "mimo_c_user_id",
)


class SecretBox:
    def __init__(self, config_path: Path):
        self.key_path = config_path.parent / ".secret_key"
        self._fernet: Optional[Fernet] = None
        self._lock = threading.RLock()

    def _load_or_create(self) -> Fernet:
        with self._lock:
            if self._fernet is not None:
                return self._fernet
            if self.key_path.exists():
                self._fernet = Fernet(self.key_path.read_bytes().strip())
                return self._fernet
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            self._fernet = Fernet(key)
            return self._fernet

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        if isinstance(plaintext, str) and plaintext.startswith(ENC_PREFIX):
            return plaintext
        return ENC_PREFIX + self._load_or_create().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
            return value
        try:
            return self._load_or_create().decrypt(value[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, Exception) as e:
            print(f"[Config] decrypt failed ({e}); check .secret_key")
            return ""


@dataclass
class MimoAccount:
    """Desktop 账号（内存明文）"""

    mimo_pass_token: str = ""
    mimo_user_id: str = ""
    mimo_c_user_id: str = ""
    uid: str = ""
    login_time: str = ""
    last_test: str = ""
    is_valid: bool = False

    def has_session(self) -> bool:
        return bool(self.mimo_pass_token)

    # ── 兼容旧 routes.py 的字段名 ──────────────────────────
    @property
    def user_id(self) -> str:
        return self.mimo_user_id or self.uid

    @property
    def service_token(self) -> str:
        """aistudio serviceToken；Desktop 通路不使用，恒为空。"""
        return ""

    @property
    def xiaomichatbot_ph(self) -> str:
        """aistudio 会话标识；Desktop 通路不使用，恒为空。"""
        return ""

    def to_masked_dict(self) -> dict:
        d = asdict(self)
        pt = self.mimo_pass_token or ""
        d["mimo_pass_token_masked"] = (pt[:8] + "..." + pt[-4:]) if len(pt) > 16 else ("***" if pt else "")
        d.pop("mimo_pass_token", None)
        d.pop("mimo_user_id", None)
        d.pop("mimo_c_user_id", None)
        return d

    def to_storage_dict(self, box: SecretBox) -> dict:
        return {
            "mimo_pass_token": box.encrypt(self.mimo_pass_token),
            "mimo_user_id": box.encrypt(self.mimo_user_id),
            "mimo_c_user_id": box.encrypt(self.mimo_c_user_id),
            "uid": self.uid,
            "login_time": self.login_time,
            "last_test": self.last_test,
            "is_valid": self.is_valid,
        }


@dataclass
class Config:
    api_keys: str = DEFAULT_API_KEYS
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    mimo_accounts: List[MimoAccount] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    tools_passthrough: bool = DEFAULT_TOOLS_PASSTHROUGH
    compression_mode: str = DEFAULT_COMPRESSION_MODE

    def to_dict(self) -> dict:
        return {
            "api_keys": self.api_keys,
            "admin_password": "***" if self.admin_password else "",
            "mimo_accounts": [a.to_masked_dict() for a in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
            "models": self.models,
        }

    def to_save_dict(self, box: SecretBox) -> dict:
        return {
            "api_keys": self.api_keys,
            "admin_password": box.encrypt(self.admin_password),
            "mimo_accounts": [a.to_storage_dict(box) for a in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
            "models": self.models,
        }


def _decrypt_account(raw: dict, box: SecretBox) -> MimoAccount:
    return MimoAccount(
        mimo_pass_token=box.decrypt(raw.get("mimo_pass_token", "")),
        mimo_user_id=box.decrypt(raw.get("mimo_user_id", "")),
        mimo_c_user_id=box.decrypt(raw.get("mimo_c_user_id", "")),
        uid=raw.get("uid") or "",
        login_time=raw.get("login_time", ""),
        last_test=raw.get("last_test", ""),
        is_valid=bool(raw.get("is_valid", False)),
    )


class ConfigManager:
    def __init__(self, config_file: str = "config.json"):
        self.config_file = Path(config_file)
        self.box = SecretBox(self.config_file)
        self.config = Config()
        self.lock = threading.RLock()
        self.account_idx = 0
        self.load()

    def load(self) -> None:
        if not self.config_file.exists():
            self.save()
            return
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            accounts = [_decrypt_account(a, self.box) for a in data.get("mimo_accounts", [])]
            self.config = Config(
                api_keys=data.get("api_keys", DEFAULT_API_KEYS),
                admin_password=self.box.decrypt(data.get("admin_password", DEFAULT_ADMIN_PASSWORD)),
                mimo_accounts=accounts,
                models=data.get("models", []),
                tools_passthrough=data.get("tools_passthrough", DEFAULT_TOOLS_PASSTHROUGH),
                compression_mode=data.get("compression_mode", DEFAULT_COMPRESSION_MODE),
            )
            if self._has_legacy_plaintext(data):
                print("[Config] migrating plaintext secrets to encrypted storage")
                self.save()
        except Exception as e:
            print(f"[Config] load failed: {e}")
            self.config = Config()
            self.save()

    @staticmethod
    def _has_legacy_plaintext(data: dict) -> bool:
        pw = data.get("admin_password")
        if isinstance(pw, str) and pw and not pw.startswith(ENC_PREFIX):
            return True
        for acc in data.get("mimo_accounts", []):
            for k in _SENSITIVE_ACCOUNT_FIELDS:
                v = acc.get(k)
                if isinstance(v, str) and v and not v.startswith(ENC_PREFIX):
                    return True
        return False

    def save(self) -> None:
        with self.lock:
            try:
                self.config_file.write_text(
                    json.dumps(self.config.to_save_dict(self.box), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                try:
                    os.chmod(self.config_file, 0o600)
                except OSError:
                    pass
            except Exception as e:
                print(f"[Config] save failed: {e}")

    def validate_api_key(self, key: str) -> bool:
        with self.lock:
            return key in [k.strip() for k in self.config.api_keys.split(",")]

    def get_next_account(self) -> Optional[MimoAccount]:
        with self.lock:
            if not self.config.mimo_accounts:
                return None
            acc = self.config.mimo_accounts[self.account_idx % len(self.config.mimo_accounts)]
            self.account_idx += 1
            return acc

    def update_config(self, new_config: dict) -> None:
        """局部更新：未传入的字段保留原值（可只改 admin_password）。"""
        with self.lock:
            if "mimo_accounts" in new_config:
                old_by_uid = {a.uid: a for a in self.config.mimo_accounts}
                accounts = []
                for acc in new_config.get("mimo_accounts") or []:
                    fields = {k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__}
                    prev = old_by_uid.get(fields.get("uid"))
                    if prev:
                        for k in _SENSITIVE_ACCOUNT_FIELDS:
                            v = fields.get(k)
                            if v in ("***", "", None) or (isinstance(v, str) and v.startswith(ENC_PREFIX)):
                                fields[k] = getattr(prev, k)
                    accounts.append(MimoAccount(**fields))
            else:
                accounts = self.config.mimo_accounts

            pw = new_config.get("admin_password", self.config.admin_password)
            if pw in ("***", "", None) or (isinstance(pw, str) and pw.startswith(ENC_PREFIX)):
                pw = self.config.admin_password

            self.config = Config(
                api_keys=new_config.get("api_keys", self.config.api_keys),
                admin_password=pw,
                mimo_accounts=accounts,
                models=new_config.get("models", self.config.models),
                tools_passthrough=new_config.get("tools_passthrough", self.config.tools_passthrough),
                compression_mode=new_config.get("compression_mode", self.config.compression_mode),
            )
            self.save()

    def get_config(self) -> dict:
        with self.lock:
            return self.config.to_dict()


config_manager = ConfigManager()
