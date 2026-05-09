import json
import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

RULES_PATH = BASE_DIR / "app" / "data" / "tds_rules_2026_27.json"

def seed_db():
    print("Checking for existing TDS rules...")
    res = supabase.table("tds_rules").select("id").limit(1).execute()
    if res.data:
        print("Data already exists in tds_rules. Skipping seed.")
        return

    print("Loading rules from JSON...")
    if not RULES_PATH.exists():
        print(f"File not found: {RULES_PATH}")
        return

    rules = json.loads(RULES_PATH.read_text())
    
    rules_data = []
    for rule in rules:
        rules_data.append({
            "return_code": rule["return_code"],
            "old_section": rule["old_section"],
            "new_section": rule["new_section"],
            "nature_of_payment": rule["nature_of_payment"],
            "rate": rule["rate"],
            "threshold": rule["threshold"],
            "category": rule["category"],
            "source_url": rule["source_url"],
            "notes": rule.get("notes", "")
        })

    print(f"Inserting {len(rules_data)} rules into Supabase...")
    # Supabase allows bulk inserts
    supabase.table("tds_rules").insert(rules_data).execute()
    
    print("Inserting default reference document...")
    supabase.table("documents").insert({
        "user_id": 101,
        "type": "default_reference",
        "title": "TDSMAN TDS/TCS Rate Chart Tax Year 2026-27",
        "source_url": "https://blog.tdsman.com/2026/03/tds-tcs-rate-chart-fy-2026-27/",
        "extracted_text": json.dumps(rules, ensure_ascii=False)
    }).execute()

    print("Seed complete!")

if __name__ == "__main__":
    seed_db()
