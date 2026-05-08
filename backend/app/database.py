from __future__ import annotations

import json
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RULES_PATH = DATA_DIR / "tds_rules_2026_27.json"

def get_db_url() -> str:
    """Returns the database URL from environment variables."""
    # Standard Vercel Postgres / Supabase env var
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "postgresql://postgres:postgres@localhost:5432/postgres"

@contextmanager
def db() -> Iterator[psycopg2.extensions.connection]:
    """Provides a transactional context for Postgres."""
    url = get_db_url()
    # Handle sslmode if needed for Supabase
    if "supabase.co" in url and "sslmode" not in url:
        url += "?sslmode=require"
        
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db() -> None:
    """Initializes the Postgres database with required tables and seed data."""
    try:
        with db() as conn:
            with conn.cursor() as cur:
                # Create users table first
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL
                    );
                """)
                
                # Ensure email column exists (for older versions of the table or existing Supabase schema)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
                
                # Try to migrate from user_email if it exists
                cur.execute("""
                    DO $$ 
                    BEGIN 
                        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='user_email') THEN
                            UPDATE users SET email = user_email WHERE email IS NULL;
                        END IF;
                    END $$;
                """)
                
                # Fill remaining nulls with unique values
                cur.execute("UPDATE users SET email = 'demo' || id || '@example.com' WHERE email IS NULL")
                
                # Now add the unique constraint if not already present
                cur.execute("""
                    DO $$ 
                    BEGIN 
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_email_key') THEN
                            ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);
                        END IF;
                    END $$;
                """)
                
                # Ensure NOT NULL
                cur.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
                
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chats (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        title TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        chat_id INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        sources TEXT DEFAULT '[]',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(chat_id) REFERENCES chats(id)
                    );

                    CREATE TABLE IF NOT EXISTS documents (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        source_url TEXT,
                        extracted_text TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    );

                    CREATE TABLE IF NOT EXISTS invoice_extractions (
                        id SERIAL PRIMARY KEY,
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
                        id SERIAL PRIMARY KEY,
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
                """)

                cur.execute("INSERT INTO users (id, name, email) VALUES (1, 'Demo User', 'demo@example.com') ON CONFLICT (id) DO NOTHING")
                
                cur.execute("SELECT COUNT(*) AS c FROM tds_rules")
                count = cur.fetchone()["c"]
                if count == 0 and RULES_PATH.exists():
                    rules = json.loads(RULES_PATH.read_text())
                    for rule in rules:
                        cur.execute("""
                            INSERT INTO tds_rules (
                                return_code, old_section, new_section, nature_of_payment,
                                rate, threshold, category, source_url, notes
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            rule["return_code"], rule["old_section"], rule["new_section"], 
                            rule["nature_of_payment"], rule["rate"], rule["threshold"], 
                            rule["category"], rule["source_url"], rule.get("notes", "")
                        ))
                    
                    cur.execute("""
                        INSERT INTO documents (user_id, type, title, source_url, extracted_text)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        1,
                        "default_reference",
                        "TDSMAN TDS/TCS Rate Chart Tax Year 2026-27",
                        "https://blog.tdsman.com/2026/03/tds-tcs-rate-chart-fy-2026-27/",
                        json.dumps(rules, ensure_ascii=False)
                    ))
    except Exception as e:
        print(f"DATABASE INITIALIZATION FAILED: {e}")
        # We don't re-raise here to allow the app to start, but subsequent DB calls will fail.
        # This is better for debugging on Vercel as it allows the /health endpoint to work.

def rows_to_dicts(rows: list) -> list[dict]:
    """Helper to convert results to dicts."""
    return [dict(row) for row in rows]

def pg_execute(conn, sql: str, params: tuple | list | None = None):
    """Shallow wrapper to mimic sqlite3's conn.execute with Postgres support."""
    sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur
