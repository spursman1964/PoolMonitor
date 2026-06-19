import csv
import io
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"

# Whitelisted metrics. Only "ph" has data today; temperature/salinity are
# wired up here so the same endpoint works once those columns are populated
# (per the roadmap), without any extra backend changes.
METRIC_COLUMNS = {
    "ph": "ph",
    "temperature": "temperature_c",
    "salinity": "salinity_ppm",
}

# Accepts things like "1h", "12h", "24h", "7d", "30d", "6m", "1y"
RANGE_PATTERN = re.compile(r"^(\d+)\s*([hdwmy])$", re.IGNORECASE)

UNIT_TO_HOURS = {
    "h": 1,
    "d": 24,
    "w": 24 * 7,
    "m": 24 * 30,   # approximate month
    "y": 24 * 365,  # approximate year
}


def query_db(query, params=()):
    connection = sqlite3.connect(DB_PATH)
    try:
        cursor = connection.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        connection.close()


def parse_range_to_hours(range_str):
    """Turn '45d' / '6m' / '1y' etc into a number of hours, or None if invalid."""
    match = RANGE_PATTERN.match((range_str or "").strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        return None
    return amount * UNIT_TO_HOURS[unit]


def choose_bucket_expression(total_hours):
    """
    Pick a candle width that fits the requested lookback window, so a 1-hour
    view doesn't collapse into a single candle and a 1-year view doesn't
    return thousands of them. This is chosen server-side from a fixed set of
    options below -- it is never built directly from user input.
    """
    if total_hours <= 2:
        return "strftime('%Y-%m-%dT%H:%M:00', timestamp)"          # 1-minute
    if total_hours <= 48:
        return "strftime('%Y-%m-%dT%H:00:00', timestamp)"           # hourly
    if total_hours <= 24 * 60:
        return "strftime('%Y-%m-%dT00:00:00', timestamp)"           # daily
    if total_hours <= 24 * 400:
        return ("strftime('%Y-%m-%dT00:00:00', "
                 "date(timestamp, 'weekday 0', '-6 days'))")          # weekly
    return "strftime('%Y-%m-01T00:00:00', timestamp)"                # monthly


@app.route("/api/ph/ohlc")
def ph_ohlc():
    range_param = request.args.get("range", "24h")
    metric_param = request.args.get("metric", "ph")

    total_hours = parse_range_to_hours(range_param)
    if total_hours is None:
        return jsonify({
            "error": "Invalid range. Use a number plus h/d/w/m/y, e.g. 1h, 12h, 24h, 7d, 30d, 6m, 1y."
        }), 400

    if metric_param not in METRIC_COLUMNS:
        return jsonify({
            "error": "Invalid metric. Choose from: " + ", ".join(METRIC_COLUMNS)
        }), 400

    column = METRIC_COLUMNS[metric_param]
    bucket = choose_bucket_expression(total_hours)
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=total_hours)).isoformat()

    rows = query_db(f"""
        WITH bucketed AS (
            SELECT
                {bucket} AS bucket_time,
                timestamp,
                {column} AS value
            FROM readings
            WHERE datetime(timestamp) >= datetime(?)
              AND {column} IS NOT NULL
        ),
        grouped AS (
            SELECT
                bucket_time,
                MIN(value) AS low,
                MAX(value) AS high,
                MIN(timestamp) AS first_ts,
                MAX(timestamp) AS last_ts
            FROM bucketed
            GROUP BY bucket_time
        )
        SELECT
            grouped.bucket_time,
            open_row.value AS open,
            grouped.high,
            grouped.low,
            close_row.value AS close
        FROM grouped
        JOIN bucketed open_row
            ON open_row.bucket_time = grouped.bucket_time
           AND open_row.timestamp = grouped.first_ts
        JOIN bucketed close_row
            ON close_row.bucket_time = grouped.bucket_time
           AND close_row.timestamp = grouped.last_ts
        ORDER BY grouped.bucket_time
    """, (cutoff,))

    data = [
        {
            "time": int(datetime.fromisoformat(row[0]).timestamp()),
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
        }
        for row in rows
    ]

    return jsonify({
        "range": range_param,
        "metric": metric_param,
        "data": data,
    })


# --- Raw data browser -------------------------------------------------------

def normalize_date_filters(start, end):
    """
    datetime-local inputs send 'YYYY-MM-DDTHH:MM' (no seconds). Pad them so
    string comparison against the full 'YYYY-MM-DDTHH:MM:SS' timestamps in
    the database behaves as the user expects at the start/end of a minute.
    """
    start = (start or "").strip()
    end = (end or "").strip()
    if start and len(start) == 16:
        start = start + ":00"
    if end and len(end) == 16:
        end = end + ":59"
    return start, end


def build_readings_query(start, end, order):
    where_clauses = []
    params = []
    if start:
        where_clauses.append("timestamp >= ?")
        params.append(start)
    if end:
        where_clauses.append("timestamp <= ?")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    order_sql = "ASC" if order == "asc" else "DESC"
    return where_sql, order_sql, params


def build_data_url(page, page_size, start, end, order):
    params = {"page": page, "page_size": page_size, "order": order}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return "/data?" + urlencode(params)


def build_export_url(start, end, order):
    params = {"order": order}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return "/data/export.csv?" + urlencode(params)


@app.route("/data")
def raw_data():
    start_raw = request.args.get("start", "")
    end_raw = request.args.get("end", "")
    start, end = normalize_date_filters(start_raw, end_raw)

    order = request.args.get("order", "desc").lower()
    order = "asc" if order == "asc" else "desc"

    try:
        page_size = int(request.args.get("page_size", 100))
    except ValueError:
        page_size = 100
    page_size = min(max(page_size, 10), 1000)

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)

    where_sql, order_sql, params = build_readings_query(start, end, order)

    total_count = query_db(f"SELECT COUNT(*) FROM readings {where_sql}", params)[0][0]
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    rows = query_db(
        f"""
        SELECT id, timestamp, ph, temperature_c, salinity_ppm
        FROM readings
        {where_sql}
        ORDER BY id {order_sql}
        LIMIT ? OFFSET ?
        """,
        params + [page_size, offset],
    )

    table_rows = "".join(
        "<tr><td>{id}</td><td>{ts}</td><td>{ph}</td><td>{temp}</td><td>{sal}</td></tr>".format(
            id=row_id,
            ts=ts,
            ph=f"{ph:.3f}" if ph is not None else "--",
            temp=f"{temp:.2f}" if temp is not None else "--",
            sal=f"{sal:.1f}" if sal is not None else "--",
        )
        for row_id, ts, ph, temp, sal in rows
    )

    return render_raw_data_page(
        table_rows=table_rows,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        page_size=page_size,
        start_value=start_raw,
        end_value=end_raw,
        order=order,
        prev_url=build_data_url(page - 1, page_size, start, end, order) if page > 1 else None,
        next_url=build_data_url(page + 1, page_size, start, end, order) if page < total_pages else None,
        first_url=build_data_url(1, page_size, start, end, order),
        last_url=build_data_url(total_pages, page_size, start, end, order),
        export_url=build_export_url(start, end, order),
    )


@app.route("/data/export.csv")
def export_csv():
    start, end = normalize_date_filters(request.args.get("start", ""), request.args.get("end", ""))
    order = request.args.get("order", "desc").lower()
    order = "asc" if order == "asc" else "desc"

    where_sql, order_sql, params = build_readings_query(start, end, order)

    rows = query_db(
        f"""
        SELECT id, timestamp, ph, temperature_c, salinity_ppm
        FROM readings
        {where_sql}
        ORDER BY id {order_sql}
        """,
        params,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "timestamp", "ph", "temperature_c", "salinity_ppm"])
    writer.writerows(rows)

    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=poolmonitor_readings.csv"
    return response


# --- Page rendering -------------------------------------------------------
# CSS/JS are kept as plain (non f-string) text so their { } braces can never
# be mis-parsed as Python expressions. Only the small render function below
# uses an f-string, and only over named variables.

DASHBOARD_CSS = """
body {
    font-family: Arial, sans-serif;
    margin: 40px;
    background: #f4f6f8;
}

.card {
    background: white;
    padding: 25px;
    border-radius: 12px;
    max-width: 700px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    margin-bottom: 25px;
}

.ph-value {
    font-size: 64px;
    font-weight: bold;
}

table {
    border-collapse: collapse;
    background: white;
}

th, td {
    padding: 10px 14px;
    border: 1px solid #ccc;
}

th {
    background: #e9ecef;
}

.range-buttons button {
    margin-right: 6px;
    margin-bottom: 8px;
    padding: 6px 12px;
    border: 1px solid #ccc;
    border-radius: 6px;
    background: #f0f0f0;
    cursor: pointer;
}

.range-buttons button.active {
    background: #1565c0;
    color: white;
    border-color: #1565c0;
}

.custom-range {
    margin-top: 6px;
}

.custom-range input {
    padding: 6px 10px;
    border: 1px solid #ccc;
    border-radius: 6px;
    margin-right: 6px;
}

.chart-status {
    color: #c62828;
    min-height: 1.2em;
}

.nav-link {
    display: inline-block;
    margin-bottom: 16px;
}

.filter-form {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 14px;
    margin-bottom: 18px;
}

.filter-form label {
    display: flex;
    flex-direction: column;
    font-size: 13px;
    color: #444;
    gap: 4px;
}

.filter-form input, .filter-form select {
    padding: 6px 8px;
    border: 1px solid #ccc;
    border-radius: 6px;
}

.filter-form button, .filter-form a.button-link {
    padding: 7px 14px;
    border-radius: 6px;
    border: 1px solid #1565c0;
    background: #1565c0;
    color: white;
    cursor: pointer;
    text-decoration: none;
    align-self: flex-end;
}

.filter-form a.reset-link {
    align-self: flex-end;
    color: #555;
}

.pagination {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 14px 0;
}

.pagination a {
    padding: 6px 12px;
    border: 1px solid #ccc;
    border-radius: 6px;
    background: #f0f0f0;
    text-decoration: none;
    color: #222;
}

.pagination span.disabled {
    padding: 6px 12px;
    border-radius: 6px;
    color: #aaa;
    border: 1px solid #eee;
}

.row-count {
    color: #555;
    font-size: 14px;
}
"""

DASHBOARD_JS = r"""
let currentRange = '24h';

function highlightActiveButton(range) {
    document.querySelectorAll('.range-buttons button').forEach(function (btn) {
        if (btn.dataset.range === range) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
}

async function loadChart(range) {
    currentRange = range;
    highlightActiveButton(range);

    const statusEl = document.getElementById('chartStatus');
    statusEl.textContent = '';

    let response;
    try {
        response = await fetch('/api/ph/ohlc?range=' + encodeURIComponent(range));
    } catch (err) {
        statusEl.textContent = 'Could not reach the server.';
        return;
    }

    const result = await response.json();

    if (result.error) {
        statusEl.textContent = result.error;
        Plotly.purge('phChart');
        return;
    }

    if (!result.data || result.data.length === 0) {
        statusEl.textContent = 'No readings in this time range yet.';
        Plotly.purge('phChart');
        return;
    }

    const x = result.data.map(r => new Date(r.time * 1000));
    const open = result.data.map(r => r.open);
    const high = result.data.map(r => r.high);
    const low = result.data.map(r => r.low);
    const close = result.data.map(r => r.close);

    const trace = {
        x: x,
        open: open,
        high: high,
        low: low,
        close: close,
        type: 'candlestick',
        name: 'pH',
        increasing: { line: { color: '#2e7d32' } },
        decreasing: { line: { color: '#c62828' } }
    };

    const layout = {
        title: 'Pool pH (' + range + ')',
        xaxis: { title: 'Time', rangeslider: { visible: false } },
        yaxis: { title: 'pH' },
        margin: { t: 40, r: 20, l: 50, b: 40 }
    };

    Plotly.react('phChart', [trace], layout, { responsive: true });
}

function loadCustomRange() {
    const input = document.getElementById('customRange');
    const value = input.value.trim();
    if (!value) {
        return;
    }
    if (!/^\d+\s*[hdwmy]$/i.test(value)) {
        document.getElementById('chartStatus').textContent =
            'Use a number plus h/d/w/m/y, e.g. 45d, 2w, 6m.';
        return;
    }
    loadChart(value);
}

setInterval(function () {
    loadChart(currentRange);
}, 10000);

loadChart('24h');
"""


def render_dashboard_page(ph_display, latest_timestamp, status_html, table_rows):
    return f"""<html>
<head>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <title>PoolMonitor</title>
    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <h1>PoolMonitor</h1>
    <a class="nav-link" href="/data">View Raw Data →</a>

    <div class="card">
        <h2>Current pH</h2>
        <div class="ph-value">{ph_display}</div>
        <p>Last reading: {latest_timestamp}</p>
    </div>

    <h2>System Status</h2>
    <table>
        <tr><th>Component</th><th>Status</th><th>Last Updated</th></tr>
        {status_html}
    </table>

    <br>

    <h2>pH Trend</h2>
    <div class="card">
        <div class="range-buttons">
            <button data-range="1h" class="active" onclick="loadChart('1h')">1H</button>
            <button data-range="6h" onclick="loadChart('6h')">6H</button>
            <button data-range="12h" onclick="loadChart('12h')">12H</button>
            <button data-range="24h" onclick="loadChart('24h')">24H</button>
            <button data-range="3d" onclick="loadChart('3d')">3D</button>
            <button data-range="7d" onclick="loadChart('7d')">1W</button>
            <button data-range="30d" onclick="loadChart('30d')">1M</button>
            <button data-range="90d" onclick="loadChart('90d')">3M</button>
            <button data-range="6m" onclick="loadChart('6m')">6M</button>
            <button data-range="1y" onclick="loadChart('1y')">1Y</button>
        </div>
        <div class="custom-range">
            <input type="text" id="customRange" placeholder="custom, e.g. 45d, 2w, 6m">
            <button onclick="loadCustomRange()">Go</button>
        </div>
        <p id="chartStatus" class="chart-status"></p>
        <div id="phChart" style="height: 420px;"></div>
    </div>

    <h2>Recent Readings</h2>
    <table>
        <tr><th>Timestamp</th><th>pH</th></tr>
        {table_rows}
    </table>

    <script>{DASHBOARD_JS}</script>
</body>
</html>"""


def render_raw_data_page(
    table_rows, page, total_pages, total_count, page_size,
    start_value, end_value, order, prev_url, next_url, first_url, last_url, export_url,
):
    desc_selected = "selected" if order == "desc" else ""
    asc_selected = "selected" if order == "asc" else ""

    size_options = "".join(
        f'<option value="{size}" {"selected" if size == page_size else ""}>{size}</option>'
        for size in (50, 100, 250, 500, 1000)
    )

    prev_html = f'<a href="{prev_url}">‹ Prev</a>' if prev_url else '<span class="disabled">‹ Prev</span>'
    next_html = f'<a href="{next_url}">Next ›</a>' if next_url else '<span class="disabled">Next ›</span>'
    first_html = f'<a href="{first_url}">« First</a>' if page > 1 else '<span class="disabled">« First</span>'
    last_html = f'<a href="{last_url}">Last »</a>' if page < total_pages else '<span class="disabled">Last »</span>'

    return f"""<html>
<head>
    <title>PoolMonitor - Raw Data</title>
    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <a class="nav-link" href="/">← Back to Dashboard</a>
    <h1>Raw Readings</h1>
    <p class="row-count">{total_count} total rows</p>

    <form method="get" action="/data" class="filter-form">
        <label>Start
            <input type="datetime-local" name="start" value="{start_value}">
        </label>
        <label>End
            <input type="datetime-local" name="end" value="{end_value}">
        </label>
        <label>Order
            <select name="order">
                <option value="desc" {desc_selected}>Newest first</option>
                <option value="asc" {asc_selected}>Oldest first</option>
            </select>
        </label>
        <label>Rows per page
            <select name="page_size">
                {size_options}
            </select>
        </label>
        <button type="submit">Apply</button>
        <a class="reset-link" href="/data">Reset</a>
        <a class="button-link" href="{export_url}">Download CSV</a>
    </form>

    <div class="pagination">
        {first_html}
        {prev_html}
        <span>Page {page} of {total_pages}</span>
        {next_html}
        {last_html}
    </div>

    <table>
        <tr><th>ID</th><th>Timestamp</th><th>pH</th><th>Temp (°C)</th><th>Salinity (ppm)</th></tr>
        {table_rows}
    </table>

    <div class="pagination">
        {first_html}
        {prev_html}
        <span>Page {page} of {total_pages}</span>
        {next_html}
        {last_html}
    </div>
</body>
</html>"""


@app.route("/")
def home():
    rows = query_db("SELECT timestamp, ph FROM readings ORDER BY id DESC LIMIT 100")
    status_rows = query_db(
        "SELECT component, status, last_updated FROM system_status ORDER BY id DESC LIMIT 5"
    )

    latest_timestamp = rows[0][0] if rows else "No readings"
    latest_ph = rows[0][1] if rows else None
    ph_display = f"{latest_ph:.2f}" if latest_ph is not None else "--"

    table_rows = "".join(
        f"<tr><td>{timestamp}</td><td>{ph:.3f}</td></tr>"
        for timestamp, ph in rows
    )

    status_html = "".join(
        f"<tr><td>{component}</td><td>{status}</td><td>{last_updated}</td></tr>"
        for component, status, last_updated in status_rows
    )

    return render_dashboard_page(ph_display, latest_timestamp, status_html, table_rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)