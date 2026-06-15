import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

connection = sqlite3.connect(DB_PATH)
cursor = connection.cursor()

cursor.execute(
    """
    INSERT INTO system_status
    (component, status, last_updated)
    VALUES (?, ?, ?)
    """,
    (
        "pH Logger",
        "Running",
        datetime.now().isoformat(timespec="seconds")
    )
)

connection.commit()
connection.close()

print("Status stored")
