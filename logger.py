import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

sys.path.append(str(PROJECT_ROOT / "sensors"))

from ph_sensor import read_ph


def store_reading(ph_value):
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
            ph_value,
        ),
    )

    connection.commit()
    connection.close()


while True:
    try:
        ph = read_ph()
        store_reading(ph)
        print(f"{datetime.now().isoformat(timespec='seconds')} pH={ph}")
    except Exception as error:
        print(f"ERROR: {error}")

    time.sleep(10)
