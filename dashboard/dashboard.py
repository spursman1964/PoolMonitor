import sqlite3
from pathlib import Path
from flask import Flask
import json

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"


def get_recent_readings(limit=100):
    connection = sqlite3.connect(DB_PATH)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT timestamp, ph
        FROM readings
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cursor.fetchall()
    connection.close()
    return rows


@app.route("/")
def home():
    rows = get_recent_readings()

    chart_labels = []
    chart_values = []

    for timestamp, ph in reversed(rows):
        chart_labels.append(timestamp[-8:])
        chart_values.append(ph)

    connection = sqlite3.connect(DB_PATH)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT component, status, last_updated
        FROM system_status
        ORDER BY id DESC
        LIMIT 5
    """)

    status_rows = cursor.fetchall()

    connection.close()

    latest_timestamp = rows[0][0] if rows else "No readings"
    latest_ph = rows[0][1] if rows else None

    ph_display = f"{latest_ph:.2f}" if latest_ph is not None else "--"

    table_rows = ""

    for timestamp, ph in rows:
        table_rows += f"""
        <tr>
            <td>{timestamp}</td>
            <td>{ph:.3f}</td>
        </tr>
        """

    status_html = ""

    for component, status, last_updated in status_rows:
        status_html += f"""
        <tr>
            <td>{component}</td>
            <td>{status}</td>
            <td>{last_updated}</td>
        </tr>
        """

    chart_labels_json = json.dumps(chart_labels)
    chart_values_json = json.dumps(chart_values)

    return f"""
    <html>
    <head>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <title>PoolMonitor</title>
        <meta http-equiv="refresh" content="10">
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 40px;
                background: #f4f6f8;
            }}

            .card {{
                background: white;
                padding: 25px;
                border-radius: 12px;
                max-width: 500px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.15);
                margin-bottom: 25px;
            }}

            .ph-value {{
                font-size: 64px;
                font-weight: bold;
            }}

            table {{
                border-collapse: collapse;
                background: white;
            }}

            th, td {{
                padding: 10px 14px;
                border: 1px solid #ccc;
            }}

            th {{
                background: #e9ecef;
            }}
        </style>
    </head>

    <body>
        <h1>PoolMonitor</h1>

        <div class="card">
            <h2>Current pH</h2>
            <div class="ph-value">{ph_display}</div>
            <p>Last reading: {latest_timestamp}</p>
        </div>

        <h2>System Status</h2>

        <table>
            <tr>
                <th>Component</th>
                <th>Status</th>
                <th>Last Updated</th>
            </tr>

            {status_html}

        </table>

        <br>

        <h2>pH Trend</h2>

        <div class="card">
            <canvas id="phChart"></canvas>
        </div>

        <h2>Recent Readings</h2>

        <table>
            <tr>
                <th>Timestamp</th>
                <th>pH</th>
            </tr>
            {table_rows}
        </table>

        <script>

        const labels = {chart_labels_json};
        const phData = {chart_values_json};

        new Chart(
            document.getElementById('phChart'),
            {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [{{
                        label: 'pH',
                        data: phData
                    }}]
                }},
                options: {{
                    responsive: true,
                    animation: false,
                    scales : {{
                                suggestedMin: 4,
                                suggestedMax: 10
                    }}
                }}
            }}
        );

        </script>


    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
