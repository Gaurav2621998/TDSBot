from __future__ import annotations

import os
from pathlib import Path
from supabase import create_client, Client
from .config import load_config

load_config()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RULES_PATH = DATA_DIR / "tds_rules_2026_27.json"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    print("WARNING: SUPABASE_URL and SUPABASE_KEY must be set in the environment.")

def init_db() -> None:
    # Schema creation is handled manually in Supabase SQL editor
    pass
