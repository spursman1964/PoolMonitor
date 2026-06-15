import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "poolmonitor.db"

connection = sqlite3.connect(DB_PATH)
cursor = connection.cursor()

cursor.execute("""
    SELECT *
    FROM readings
    ORDER BY id DESC
    LIMIT 10
""")

rows = cursor.fetchall()

for row in rows:
    print(row)

connection.close()
