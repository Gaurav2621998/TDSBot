from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BACKEND_DIR / ".env"


def load_config() -> None:
    load_dotenv(ENV_PATH, override=False)


def get_setting(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def has_real_setting(name: str) -> bool:
    value = (get_setting(name) or "").strip()
    return bool(value and not value.startswith("your_"))
