from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tdsbot.sqlite3"
RULES_PATH = DATA_DIR / "tds_rules_2026_27.json"


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT,
                extracted_text TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS invoice_extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                vendor_name TEXT,
                invoice_number TEXT,
                invoice_date TEXT,
                amount TEXT,
                gst_details TEXT,
                party_details TEXT,
                items_json TEXT DEFAULT '[]',
                extracted_text TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS tds_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_code TEXT NOT NULL,
                old_section TEXT NOT NULL,
                new_section TEXT NOT NULL,
                nature_of_payment TEXT NOT NULL,
                rate TEXT NOT NULL,
                threshold TEXT NOT NULL,
                category TEXT NOT NULL,
                source_url TEXT NOT NULL,
                notes TEXT DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, name, email) VALUES (1, 'Demo User', 'demo@example.com')"
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM tds_rules").fetchone()["c"]
        if count == 0:
            rules = json.loads(RULES_PATH.read_text())
            conn.executemany(
                """
                INSERT INTO tds_rules (
                    return_code, old_section, new_section, nature_of_payment,
                    rate, threshold, category, source_url, notes
                ) VALUES (
                    :return_code, :old_section, :new_section, :nature_of_payment,
                    :rate, :threshold, :category, :source_url, :notes
                )
                """,
                rules,
            )
            conn.execute(
                """
                INSERT INTO documents (user_id, type, title, source_url, extracted_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "default_reference",
                    "TDSMAN TDS/TCS Rate Chart Tax Year 2026-27",
                    "https://blog.tdsman.com/2026/03/tds-tcs-rate-chart-fy-2026-27/",
                    json.dumps(rules, ensure_ascii=False),
                ),
            )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
