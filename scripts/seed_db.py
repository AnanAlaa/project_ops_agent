"""Reset the demo database: python scripts/seed_db.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import reset_database  # noqa: E402

if __name__ == "__main__":
    conn = reset_database()
    n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    print(f"Database reset with {n} tasks.")
