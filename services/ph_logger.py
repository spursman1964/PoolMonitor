import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"

with open(CONFIG_PATH, "r") as file:
    settings = yaml.safe_load(file)

LOG_INTERVAL = settings["logging"]["interval_seconds"]

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

        print(
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"pH={ph}"
        )

    except Exception as error:

        print(f"ERROR: {error}")

    time.sleep(LOG_INTERVAL)
