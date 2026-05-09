"""
Seeds the TDS Act 2025 expert reference MD file into the documents table
as a default_reference so it is always available to the LLM.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MD_PATH = BASE_DIR / "app" / "data" / "tds_new_income_tax_act_2025_reference-(1).md"
QA_PATH = BASE_DIR / "app" / "data" / "tds_qa_training_dataset-(1).json"


def seed():
    # --- Reference MD ---
    print("Removing old 'TDS Act 2025 Expert Reference' document if present...")
    supabase.table("documents").delete().eq("title", "TDS Act 2025 Expert Reference").execute()

    md_text = MD_PATH.read_text(encoding="utf-8")
    print(f"Read {len(md_text):,} chars from MD file. Inserting...")
    supabase.table("documents").insert({
        "user_id": 101,
        "type": "default_reference",
        "title": "TDS Act 2025 Expert Reference",
        "source_url": "tds_new_income_tax_act_2025_reference.md",
        "extracted_text": md_text,
    }).execute()
    print("  ✓ Reference MD inserted.")

    # --- Income Tax Act Extract (already seeded; refresh if outdated) ---
    existing = supabase.table("documents").select("id").eq("title", "Income Tax Act 2025 (TDS Extracts)").execute()
    if existing.data:
        print("  ✓ Income Tax Act 2025 TDS Extracts already present — skipping.")
    else:
        print("  ℹ Income Tax Act extract not found. Run scratch_insert_tds.py to seed it.")

    print("\n✅ Done!")


if __name__ == "__main__":
    seed()
