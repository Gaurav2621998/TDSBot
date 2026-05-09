"""
Seed all 92 TDS return codes from tds_return_codes_mapping.csv into the tds_rules table.
This replaces the existing 17 old-style rules with the full new Act 2025 code set.
"""
import csv
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

CSV_PATH = BASE_DIR / "app" / "data" / "tds_return_codes_mapping.csv"
SOURCE_URL = "https://blog.tdsman.com/2026/03/tds-tcs-rate-chart-fy-2026-27/"


def derive_category(nature: str, deductee_cat: str) -> str:
    """Map CSV nature_of_payment + deductee_category to an ALLOWED_CATEGORY."""
    n = nature.lower()
    if "N/A (TCS)" in deductee_cat:
        return "TCS applicable sale category"
    if deductee_cat.strip().lower() == "non-resident":
        return "non-resident/foreign payment"
    if "salary" in n or "epf" in n:
        return "salary"
    if "immovable property" in n or "compulsory acquisition" in n:
        return "purchase of property"
    if "winnings" in n or "lottery" in n or "horse race" in n or "online game" in n:
        return "winnings"
    if "rent" in n:
        return "rent"
    if "interest" in n:
        return "interest"
    if "contractor" in n or "work" in n:
        return "contractor payment"
    if "technical" in n or "fts" in n:
        return "technical services"
    if "professional" in n or "director" in n or "royalty" in n or "consultancy" in n:
        return "professional fees"
    if "insurance commission" in n:
        return "insurance commission"
    if "commission" in n or "brokerage" in n:
        return "commission or brokerage"
    if "dividend" in n:
        return "interest"
    if "e-commerce" in n:
        return "e-commerce transaction"
    if "purchase of goods" in n or "purchase of any goods" in n:
        return "purchase of goods"
    return "other payments"



def seed():
    print("Clearing existing tds_rules rows...")
    supabase.table("tds_rules").delete().neq("id", 0).execute()
    print("Cleared.")

    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            nature = row["nature_of_payment"].strip()
            deductee = row["deductee_category"].strip()
            return_form = row["return_form"].strip()
            raw_notes = row.get("notes", "").strip()

            # Pack return_form + deductee_category into notes so no schema change needed
            notes_parts = []
            if raw_notes:
                notes_parts.append(raw_notes)
            notes_parts.append(f"Return form: {return_form}")
            notes_parts.append(f"Deductee: {deductee}")

            rows.append({
                "return_code": row["code"].strip(),
                "old_section": row["old_section"].strip(),
                "new_section": row["new_section_reference"].strip(),
                "nature_of_payment": nature,
                "rate": row["rate"].strip(),
                "threshold": row["threshold"].strip(),
                "category": derive_category(nature, deductee),
                "source_url": SOURCE_URL,
                "notes": " | ".join(notes_parts),
            })

    print(f"Inserting {len(rows)} rows in batches...")
    batch_size = 20
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        supabase.table("tds_rules").insert(batch).execute()
        print(f"  ✓ Rows {i + 1}–{min(i + batch_size, len(rows))}")

    print(f"\n✅ Done! Seeded {len(rows)} return codes into tds_rules.")


if __name__ == "__main__":
    seed()
