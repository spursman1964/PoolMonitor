import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

sys.path.append(str(PROJECT_ROOT / "sensors"))

from ph_sensor import read_ph

ph_value = read_ph()

connection = sqlite3.connect(DB_PATH)
cursor = connection.cursor()

cursor.execute(
    """
    INSERT INTO readings
    (timestamp, ph)
    VALUES (?, ?)
    """,
    (
        datetime.now().isoformat(timespec="seconds"),
        ph_value
    )
)

connection.commit()
connection.close()

print("Reading stored")
