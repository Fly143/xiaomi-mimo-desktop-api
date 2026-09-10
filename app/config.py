"""配置管理 — Desktop 版

账号凭证结构：
  mimoPassToken / mimoUserId / mimoCUserId  — Desktop passToken（会话续期）
  apiKey                                     — sk- 官方 API key（可选，稳定模型）
  uid                                        — 小米 uid
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_API_KEYS = "sk-mimo"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_TOOLS_PASSTHROUGH = False
DEFAULT_COMPRESSION_MODE = "compress"


@dataclass
class MimoAccount:
    """Desktop / 小米账号"""

    # 会话凭证（二选一或并存）
    mimo_pass_token: str = ""
    mimo_user_id: str = ""
    mimo_c_user_id: str = ""
    api_key: str = ""  # sk-xxx，官方 OpenAI 兼容 API

    # 元数据
    uid: str = ""
    base_url: str = "https://api.xiaomimimo.com/v1"
    login_time: str = ""
    last_test: str = ""
    is_valid: bool = False

    def has_session(self) -> bool:
        return bool(self.mimo_pass_token)

    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.startswith("sk-"))

    def to_dict(self) -> dict:
        d = asdict(self)
        pt = d.get("mimo_pass_token") or ""
        d["mimo_pass_token_masked"] = (pt[:8] + "..." + pt[-4:]) if len(pt) > 16 else ("***" if pt else "")
        key = d.get("api_key") or ""
        d["api_key_masked"] = (key[:8] + "..." + key[-4:]) if len(key) > 16 else ("***" if key else "")
        d.pop("mimo_pass_token", None)
        d.pop("api_key", None)
        return d


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
            "admin_password": self.admin_password,
            "mimo_accounts": [a.to_dict() for a in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
            "models": self.models,
        }

    def to_save_dict(self) -> dict:
        return {
            "api_keys": self.api_keys,
            "admin_password": self.admin_password,
            "mimo_accounts": [
                {k: v for k, v in asdict(a).items()}
                for a in self.mimo_accounts
            ],
            "tools_passthrough": self.tools_passthrough,
            "compression_mode": self.compression_mode,
            "models": self.models,
        }


_ACCOUNT_FIELDS = set(MimoAccount.__dataclass_fields__.keys())


def _parse_account(raw: dict) -> MimoAccount:
    # 兼容旧字段名 serviceToken → 不再使用；新字段 snake_case
    return MimoAccount(**{k: v for k, v in raw.items() if k in _ACCOUNT_FIELDS})


class ConfigManager:
    def __init__(self, config_file: str = "config.json"):
        self.config_file = Path(config_file)
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
            accounts = [_parse_account(a) for a in data.get("mimo_accounts", [])]
            self.config = Config(
                api_keys=data.get("api_keys", DEFAULT_API_KEYS),
                admin_password=data.get("admin_password", DEFAULT_ADMIN_PASSWORD),
                mimo_accounts=accounts,
                models=data.get("models", []),
                tools_passthrough=data.get("tools_passthrough", DEFAULT_TOOLS_PASSTHROUGH),
                compression_mode=data.get("compression_mode", DEFAULT_COMPRESSION_MODE),
            )
        except Exception as e:
            print(f"[Config] load failed: {e}")
            self.config = Config()
            self.save()

    def save(self) -> None:
        with self.lock:
            try:
                self.config_file.write_text(
                    json.dumps(self.config.to_save_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"[Config] save failed: {e}")

    def validate_api_key(self, key: str) -> bool:
        with self.lock:
            keys = [k.strip() for k in self.config.api_keys.split(",")]
            return key in keys

    def get_next_account(self) -> Optional[MimoAccount]:
        with self.lock:
            if not self.config.mimo_accounts:
                return None
            acc = self.config.mimo_accounts[self.account_idx % len(self.config.mimo_accounts)]
            self.account_idx += 1
            return acc

    def update_config(self, new_config: dict) -> None:
        with self.lock:
            accounts = [_parse_account(a) for a in new_config.get("mimo_accounts", [])]
            self.config = Config(
                api_keys=new_config.get("api_keys", DEFAULT_API_KEYS),
                admin_password=new_config.get("admin_password", DEFAULT_ADMIN_PASSWORD),
                mimo_accounts=accounts,
                models=new_config.get("models", []),
                tools_passthrough=new_config.get("tools_passthrough", DEFAULT_TOOLS_PASSTHROUGH),
                compression_mode=new_config.get("compression_mode", DEFAULT_COMPRESSION_MODE),
            )
            self.save()

    def get_config(self) -> dict:
        with self.lock:
            return self.config.to_dict()


config_manager = ConfigManager()
