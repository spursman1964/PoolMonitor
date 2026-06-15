import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "poolmonitor.db"

def initialise_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DB_PATH)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ph REAL,
            temperature_c REAL,
            salinity_ppm REAL,
            notes TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            component TEXT NOT NULL,
            status TEXT NOT NULL,
            last_updated TEXT NOT NULL
            )
    """)

    connection.commit()
    connection.close()

    print(f"Database created at: {DB_PATH}")

if __name__ == "__main__":
    initialise_database()
