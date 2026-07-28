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
from temperature_sensor import read_temperature
from ec_sensor import read_salinity_ppm


def store_reading(ph_value, temperature_value, salinity_value):

    connection = sqlite3.connect(DB_PATH)
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO readings
        (timestamp, ph, temperature_c, salinity_ppm)
        VALUES (?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            ph_value,
            temperature_value,
            salinity_value,
        ),
    )

    connection.commit()
    connection.close()


while True:

    ph = None
    temperature = None
    salinity = None

    try:
        ph = read_ph()
    except Exception as error:
        print(f"ERROR reading pH: {error}")

    try:
        temperature = read_temperature()
    except Exception as error:
        print(f"ERROR reading temperature: {error}")

    try:
        # Pass temperature for compensation if available
        salinity = read_salinity_ppm(temperature_c=temperature)
    except Exception as error:
        print(f"ERROR reading EC: {error}")

    if ph is not None or temperature is not None or salinity is not None:
        try:
            store_reading(ph, temperature, salinity)

            print(
                f"{datetime.now().isoformat(timespec='seconds')} "
                f"pH={ph} temp_c={temperature} salinity_ppm={salinity}"
            )
        except Exception as error:
            print(f"ERROR storing reading: {error}")

    time.sleep(LOG_INTERVAL)