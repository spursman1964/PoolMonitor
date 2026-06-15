import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

connection = sqlite3.connect(DB_PATH)
cursor = connection.cursor()

cursor.execute("""
    SELECT *
    FROM system_status
    ORDER BY id DESC
""")

for row in cursor.fetchall():
    print(row)

connection.close()
