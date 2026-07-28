import csv
import io
import re
import sqlite3
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# Bump this whenever dashboard.py changes. Shown in the top bar so you can
# confirm at a glance whether the browser/service is serving the latest code.
DASHBOARD_VERSION = "2.2.0 (salinity tile and table)"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poolmonitor.db"
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"

try:
    with open(CONFIG_PATH, "r") as _file:
        _settings = yaml.safe_load(_file) or {}
except FileNotFoundError:
    _settings = {}

_targets = _settings.get("targets", {})

# Single source of truth for every sensor shown on the dashboard. To add a
# new sensor (e.g. ORP, flow), add one entry here -- the metric tiles, the
# chart's metric switcher, and the OHLC API all key off this list, so no
# other rendering code needs to change.
METRICS = [
    {
        "id": "ph",
        "label": "pH",
        "column": "ph",
        "unit": "",
        "decimals": 2,
        "low": _targets.get("ph_low"),
        "high": _targets.get("ph_high"),
    },
    {
        "id": "temperature",
        "label": "Temperature",
        "column": "temperature_c",
        "unit": "\u00b0C",
        "decimals": 1,
        "low": _targets.get("temp_low"),
        "high": _targets.get("temp_high"),
    },
    {
        "id": "salinity",
        "label": "Salinity",
        "column": "salinity_ppm",
        "unit": "ppm",
        "decimals": 0,
        "low": _targets.get("salt_target", 0) * 0.9 if _targets.get("salt_target") else None,
        "high": _targets.get("salt_target", 0) * 1.1 if _targets.get("salt_target") else None,
    },
]

METRIC_COLUMNS = {m["id"]: m["column"] for m in METRICS}
METRIC_BY_ID = {m["id"]: m for m in METRICS}

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
    Target approximately 100 candles per view by dividing the total window
    (in minutes) by 100, then rounding UP to the nearest step on a fixed
    ladder of clean SQLite-expressible intervals.

    The ladder (in minutes):
        1, 2, 5, 10, 15, 30, 60, 120, 240, 360, 720, 1440, 10080, 43200

    Example outputs at the preset range buttons:
        1H   ->  1 min  (60 candles)
        6H   -> 10 min  (36 candles)
        12H  -> 10 min  (72 candles)
        24H  -> 15 min  (96 candles)
        3D   ->  1 hour (72 candles)
        1W   ->  1 hour (168 candles)
        1M   ->  6 hour (120 candles)
        3M   ->  1 day  (90 candles)
        6M   ->  1 day  (180 candles)
        1Y   ->  1 week (52 candles)

    Expressions are chosen from a fixed whitelist -- never built from user
    input -- so there is no SQL injection risk.
    """
    LADDER = [1, 2, 5, 10, 15, 30, 60, 120, 240, 360, 720, 1440, 10080, 43200]

    total_minutes = total_hours * 60
    ideal = total_minutes / 100          # target 100 candles
    # Pick the first ladder step >= ideal (round up for more definition).
    # Then walk back down if the result gives fewer than 40 candles (too sparse).
    idx = next((i for i, s in enumerate(LADDER) if s >= ideal), len(LADDER) - 1)
    while idx > 0 and (total_minutes // LADDER[idx]) < 40:
        idx -= 1
    bucket_minutes = LADDER[idx]

    # Map the chosen bucket width to a SQLite strftime expression.
    # SQLite has no native "truncate to N minutes" function, so for
    # sub-hour intervals we use integer arithmetic on the Unix epoch
    # via the julianday trick, then format back to ISO text.
    if bucket_minutes < 60:
        seconds = bucket_minutes * 60
        # Round the epoch to the nearest bucket then format as ISO.
        # This expression is safe: `seconds` is an integer from our whitelist.
        return (
            f"strftime('%Y-%m-%dT%H:%M:00', "
            f"datetime(CAST((strftime('%s', timestamp) / {seconds}) AS INTEGER)"
            f" * {seconds}, 'unixepoch'))"
        )
    elif bucket_minutes < 1440:
        hours = bucket_minutes // 60
        if hours == 1:
            return "strftime('%Y-%m-%dT%H:00:00', timestamp)"
        # Round down to nearest N-hour block using the same epoch trick.
        seconds = hours * 3600
        return (
            f"strftime('%Y-%m-%dT%H:00:00', "
            f"datetime(CAST((strftime('%s', timestamp) / {seconds}) AS INTEGER)"
            f" * {seconds}, 'unixepoch'))"
        )
    elif bucket_minutes == 1440:
        return "strftime('%Y-%m-%dT00:00:00', timestamp)"        # daily
    elif bucket_minutes == 10080:
        return (                                                   # weekly (Mon)
            "strftime('%Y-%m-%dT00:00:00', "
            "date(timestamp, 'weekday 0', '-6 days'))"
        )
    else:
        return "strftime('%Y-%m-01T00:00:00', timestamp)"         # monthly


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
:root {
    --ink: #0F1B22;
    --panel: #16252D;
    --panel-raised: #1C2E37;
    --border: #283940;
    --text: #E8EDEE;
    --text-muted: #8FA3AA;
    --accent: #4FD1C5;
    --warn: #E2A33D;
    --alarm: #E5484D;
}

* {
    box-sizing: border-box;
}

body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    margin: 0;
    padding: 20px 20px 60px;
    background: var(--ink);
    color: var(--text);
}

a {
    color: var(--accent);
}

.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 22px;
}

.topbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.live-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 0 rgba(79, 209, 197, 0.6);
    animation: pulse 2s infinite;
    flex-shrink: 0;
}

@keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(79, 209, 197, 0.5); }
    70%  { box-shadow: 0 0 0 7px rgba(79, 209, 197, 0); }
    100% { box-shadow: 0 0 0 0 rgba(79, 209, 197, 0); }
}

h1 {
    font-size: 19px;
    font-weight: 700;
    letter-spacing: 0.02em;
    margin: 0;
}

.nav-link {
    font-size: 13px;
    text-decoration: none;
    color: var(--text-muted);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 6px;
}

.nav-link:hover {
    color: var(--accent);
    border-color: var(--accent);
}

.version-tag {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    white-space: nowrap;
}

h2 {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    margin: 0 0 12px;
}

.panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 22px;
}

/* --- Instrument tiles --- */

.tile-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
    margin-bottom: 22px;
}

.tile {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px 14px 18px;
    position: relative;
    overflow: hidden;
}

.tile::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
    width: 4px;
    background: var(--status-color, var(--text-muted));
}

.tile-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    margin-bottom: 6px;
}

.tile-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 32px;
    font-weight: 600;
    line-height: 1.1;
    color: var(--text);
}

.tile-unit {
    font-size: 16px;
    color: var(--text-muted);
    margin-left: 3px;
}

.tile-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 8px;
}

.tile-time {
    font-size: 11px;
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
}

.status-pill {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 7px;
    border-radius: 999px;
    color: var(--ink);
    background: var(--status-color, var(--text-muted));
}

/* --- Tables --- */

table {
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
}

th, td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
}

th {
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.05em;
}

td {
    font-family: 'JetBrains Mono', monospace;
}

tr:hover td {
    background: var(--panel-raised);
}

.status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-right: 6px;
    background: var(--accent);
}

/* --- Chart controls --- */

.metric-buttons, .range-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
}

.metric-buttons button, .range-buttons button {
    font-family: inherit;
    font-size: 12px;
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-raised);
    color: var(--text-muted);
    cursor: pointer;
}

.metric-buttons button.active, .range-buttons button.active {
    background: var(--accent);
    color: var(--ink);
    border-color: var(--accent);
    font-weight: 600;
}

.metric-buttons button:hover, .range-buttons button:hover {
    border-color: var(--accent);
    color: var(--text);
}

.custom-range {
    margin-bottom: 10px;
}

.custom-range input {
    font-family: inherit;
    font-size: 12px;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-raised);
    color: var(--text);
    margin-right: 6px;
}

.custom-range button {
    font-family: inherit;
    font-size: 12px;
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-raised);
    color: var(--text-muted);
    cursor: pointer;
}

.chart-status {
    color: var(--alarm);
    min-height: 1.2em;
    font-size: 13px;
}

/* --- Raw data filter form --- */

.filter-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 14px;
    margin-bottom: 16px;
}

.filter-form label {
    display: flex;
    flex-direction: column;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    gap: 5px;
}

.filter-form input, .filter-form select {
    font-family: inherit;
    padding: 7px 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-raised);
    color: var(--text);
}

.filter-form button, .filter-form a.button-link {
    font-family: inherit;
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid var(--accent);
    background: var(--accent);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
}

.filter-form a.reset-link {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 13px;
    padding-bottom: 9px;
}

.pagination {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 14px 0;
    font-size: 13px;
}

.pagination a {
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-raised);
    text-decoration: none;
    color: var(--text);
}

.pagination span.disabled {
    padding: 6px 12px;
    border-radius: 6px;
    color: var(--text-muted);
    border: 1px solid var(--border);
}

.row-count {
    color: var(--text-muted);
    font-size: 13px;
}

.empty-state {
    color: var(--text-muted);
    font-size: 13px;
    padding: 10px 0;
}

/* --- Mobile --- */

@media (max-width: 600px) {
    body {
        padding: 14px 14px 50px;
    }
    .tile-value {
        font-size: 26px;
    }
    .panel {
        padding: 14px;
    }
    .filter-form {
        flex-direction: column;
        align-items: stretch;
    }
}
"""

DASHBOARD_JS = r"""
let currentRange = '24h';
let currentMetric = 'ph';

const METRIC_LABELS = {};
document.querySelectorAll('[data-metric-label]').forEach(function (el) {
    METRIC_LABELS[el.dataset.metric] = el.dataset.metricLabel;
});

function highlightActiveButton(range) {
    document.querySelectorAll('.range-buttons button').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.range === range);
    });
}

function highlightActiveMetric(metric) {
    document.querySelectorAll('.metric-buttons button').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.metric === metric);
    });
}

function setMetric(metric) {
    currentMetric = metric;
    highlightActiveMetric(metric);
    loadChart(currentRange);
}

async function loadChart(range) {
    currentRange = range;
    highlightActiveButton(range);

    const statusEl = document.getElementById('chartStatus');
    statusEl.textContent = '';

    let response;
    try {
        response = await fetch(
            '/api/ph/ohlc?range=' + encodeURIComponent(range) +
            '&metric=' + encodeURIComponent(currentMetric)
        );
    } catch (err) {
        statusEl.textContent = 'Could not reach the server.';
        return;
    }

    const result = await response.json();

    if (result.error) {
        statusEl.textContent = result.error;
        Plotly.purge('trendChart');
        return;
    }

    if (!result.data || result.data.length === 0) {
        statusEl.textContent = 'No readings in this time range yet.';
        Plotly.purge('trendChart');
        return;
    }

    const x = result.data.map(r => new Date(r.time * 1000));
    const open = result.data.map(r => r.open);
    const high = result.data.map(r => r.high);
    const low = result.data.map(r => r.low);
    const close = result.data.map(r => r.close);

    const label = METRIC_LABELS[currentMetric] || currentMetric;

    const trace = {
        x: x, open: open, high: high, low: low, close: close,
        type: 'candlestick',
        name: label,
        increasing: { line: { color: '#4FD1C5' } },
        decreasing: { line: { color: '#E5484D' } }
    };

    const layout = {
        paper_bgcolor: '#16252D',
        plot_bgcolor: '#16252D',
        font: { color: '#8FA3AA', family: 'Inter, sans-serif', size: 11 },
        title: { text: label + ' \u00b7 ' + range, font: { color: '#E8EDEE', size: 14 } },
        xaxis: { title: '', rangeslider: { visible: false }, gridcolor: '#283940', linecolor: '#283940' },
        yaxis: { title: '', gridcolor: '#283940', linecolor: '#283940' },
        margin: { t: 36, r: 16, l: 44, b: 32 }
    };

    Plotly.react('trendChart', [trace], layout, { responsive: true, displayModeBar: false });
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


def render_metric_tile(metric, latest_value, latest_timestamp):
    status_color = "var(--text-muted)"
    status_label = "NO DATA"

    if latest_value is not None:
        low, high = metric["low"], metric["high"]
        if low is not None and high is not None:
            if low <= latest_value <= high:
                status_color = "var(--accent)"
                status_label = "IN RANGE"
            else:
                status_color = "var(--warn)"
                status_label = "OUT OF RANGE"
        else:
            status_color = "var(--accent)"
            status_label = "LIVE"

    if latest_value is not None:
        value_str = f"{latest_value:.{metric['decimals']}f}"
    else:
        value_str = "--"

    unit_html = f'<span class="tile-unit">{metric["unit"]}</span>' if metric["unit"] else ""
    time_str = latest_timestamp if latest_value is not None else "--"

    return f"""
    <div class="tile" style="--status-color: {status_color};">
        <div class="tile-label">{metric['label']}</div>
        <div class="tile-value">{value_str}{unit_html}</div>
        <div class="tile-meta">
            <span class="tile-time">{time_str}</span>
            <span class="status-pill" style="--status-color: {status_color};">{status_label}</span>
        </div>
    </div>
    """


def render_metric_buttons():
    buttons = []
    for i, metric in enumerate(METRICS):
        active = "active" if i == 0 else ""
        buttons.append(
            f'<button data-metric="{metric["id"]}" class="{active}" '
            f'onclick="setMetric(\'{metric["id"]}\')">{metric["label"]}</button>'
        )
        buttons.append(
            f'<span data-metric="{metric["id"]}" data-metric-label="{metric["label"]}" style="display:none;"></span>'
        )
    return "".join(buttons)


def render_dashboard_page(tiles_html, status_html, table_rows, server_time, has_readings):
    table_block = (
        '<table><tr><th>Timestamp</th><th>pH</th><th>Temp (&deg;C)</th><th>Salinity (ppm)</th></tr>' + table_rows + '</table>'
        if has_readings else
        '<p class="empty-state">No readings yet. Once the logger starts writing data, recent readings will appear here.</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <title>PoolMonitor</title>
    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <div class="topbar">
        <div class="topbar-left">
            <span class="live-dot"></span>
            <h1>PoolMonitor</h1>
            <a class="nav-link" href="/data">Raw Data</a>
        </div>
        <div class="version-tag">v{DASHBOARD_VERSION} &middot; {server_time}</div>
    </div>

    <div class="tile-grid">
        {tiles_html}
    </div>

    <div class="panel">
        <h2>System Status</h2>
        <table>
            <tr><th>Component</th><th>Status</th><th>Last Updated</th></tr>
            {status_html}
        </table>
    </div>

    <div class="panel">
        <h2>Trends</h2>
        <div class="metric-buttons">
            {render_metric_buttons()}
        </div>
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
        <div id="trendChart" style="height: 360px;"></div>
    </div>

    <div class="panel">
        <h2>Recent Readings</h2>
        {table_block}
    </div>

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

    prev_html = f'<a href="{prev_url}">&lsaquo; Prev</a>' if prev_url else '<span class="disabled">&lsaquo; Prev</span>'
    next_html = f'<a href="{next_url}">Next &rsaquo;</a>' if next_url else '<span class="disabled">Next &rsaquo;</span>'
    first_html = f'<a href="{first_url}">&laquo; First</a>' if page > 1 else '<span class="disabled">&laquo; First</span>'
    last_html = f'<a href="{last_url}">Last &raquo;</a>' if page < total_pages else '<span class="disabled">Last &raquo;</span>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
    <title>PoolMonitor &middot; Raw Data</title>
    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <div class="topbar">
        <div class="topbar-left">
            <span class="live-dot"></span>
            <h1>Raw Readings</h1>
            <a class="nav-link" href="/">&larr; Dashboard</a>
        </div>
        <div class="version-tag">v{DASHBOARD_VERSION} &middot; {datetime.now().isoformat(timespec='seconds')}</div>
    </div>

    <div class="panel">
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
            <tr><th>ID</th><th>Timestamp</th><th>pH</th><th>Temp (&deg;C)</th><th>Salinity (ppm)</th></tr>
            {table_rows}
        </table>

        <div class="pagination">
            {first_html}
            {prev_html}
            <span>Page {page} of {total_pages}</span>
            {next_html}
            {last_html}
        </div>
    </div>
</body>
</html>"""


@app.route("/")
def home():
    rows = query_db(
        "SELECT timestamp, ph, temperature_c, salinity_ppm FROM readings ORDER BY id DESC LIMIT 100"
    )
    status_rows = query_db(
        "SELECT component, status, last_updated FROM system_status ORDER BY id DESC LIMIT 5"
    )

    latest_timestamp = rows[0][0] if rows else None
    latest_ph = rows[0][1] if rows else None
    latest_temp = rows[0][2] if rows else None
    latest_sal = rows[0][3] if rows else None

    latest_values = {"ph": latest_ph, "temperature": latest_temp, "salinity": latest_sal}

    tiles_html = "".join(
        render_metric_tile(metric, latest_values.get(metric["id"]), latest_timestamp or "--")
        for metric in METRICS
    )

    table_rows = "".join(
        "<tr><td>{ts}</td><td>{ph}</td><td>{temp}</td><td>{sal}</td></tr>".format(
            ts=timestamp,
            ph=f"{ph:.3f}" if ph is not None else "--",
            temp=f"{temp:.2f}" if temp is not None else "--",
            sal=f"{sal:.0f}" if sal is not None else "--",
        )
        for timestamp, ph, temp, sal in rows
    )

    if status_rows:
        status_html = "".join(
            f'<tr><td><span class="status-dot"></span>{component}</td>'
            f'<td>{status}</td><td>{last_updated}</td></tr>'
            for component, status, last_updated in status_rows
        )
    else:
        status_html = '<tr><td colspan="3" class="empty-state">No status reports yet.</td></tr>'

    return render_dashboard_page(
        tiles_html, status_html, table_rows,
        server_time=datetime.now().isoformat(timespec="seconds"),
        has_readings=bool(rows),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
