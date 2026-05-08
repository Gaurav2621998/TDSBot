import os
from dotenv import load_dotenv
import psycopg2
from pathlib import Path

def test_db():
    env_path = Path(__file__).parent / "backend" / ".env"
    load_dotenv(env_path)
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL not found in .env")
        return
    
    if "supabase.co" in url and "sslmode" not in url:
        url += "?sslmode=require"
        
    print(f"Connecting to {url.split('@')[-1]}...")
    try:
        conn = psycopg2.connect(url)
        print("Successfully connected to the database!")
        cur = conn.cursor()
        cur.execute("SELECT 1")
        print("Successfully executed query!")
        conn.close()
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    test_db()
