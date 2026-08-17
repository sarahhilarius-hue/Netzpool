from flask import Flask, render_template, request
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)

DB = "/homeassistant/home-assistant_v2.db"
TZ = ZoneInfo("Europe/Berlin")
DEFAULT_ENTITY = "sensor.shellyplus1pm_345f452134cc_switch_0_energy"


def get_connection():
    if not os.path.exists(DB):
        raise FileNotFoundError(f"Recorder-Datenbank nicht gefunden: {DB}")
    return sqlite3.connect(DB)


def get_energy_sensors():
    con = get_connection()
    cur = con.cursor()
    cur.execute("""
        SELECT DISTINCT sm.entity_id
        FROM states_meta sm
        JOIN states s ON s.metadata_id = sm.metadata_id
        WHERE sm.entity_id LIKE 'sensor.%'
          AND sm.entity_id LIKE '%energy%'
        ORDER BY sm.entity_id
    """)
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows


def metadata_id(cur, entity_id):
    cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id=?", (entity_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Sensor nicht im Recorder gefunden: {entity_id}")
    return row[0]


def value_at(cur, mid, dt):
    """Linear interpolation around the requested local time."""
    ts = dt.timestamp()

    cur.execute("""
        SELECT last_updated_ts, state
        FROM states
        WHERE metadata_id=?
          AND state NOT IN ('unknown','unavailable')
          AND last_updated_ts <= ?
        ORDER BY last_updated_ts DESC
        LIMIT 1
    """, (mid, ts))
    before = cur.fetchone()

    cur.execute("""
        SELECT last_updated_ts, state
        FROM states
        WHERE metadata_id=?
          AND state NOT IN ('unknown','unavailable')
          AND last_updated_ts >= ?
        ORDER BY last_updated_ts ASC
        LIMIT 1
    """, (mid, ts))
    after = cur.fetchone()

    if not before or not after:
        return None

    try:
        t1, v1 = float(before[0]), float(before[1])
        t2, v2 = float(after[0]), float(after[1])
    except (ValueError, TypeError):
        return None

    if t2 == t1:
        return v1

    ratio = (ts - t1) / (t2 - t1)
    return v1 + ratio * (v2 - v1)


def calculate(entity, start_date, end_date, start_time, end_time):
    con = get_connection()
    cur = con.cursor()
    mid = metadata_id(cur, entity)

    result = []
    total = 0.0
    day = start_date

    while day <= end_date:
        start_dt = datetime.combine(day, start_time, TZ)
        end_dt = datetime.combine(day, end_time, TZ)

        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        v_start = value_at(cur, mid, start_dt)
        v_end = value_at(cur, mid, end_dt)

        consumption = None
        if v_start is not None and v_end is not None:
            consumption = v_end - v_start
            if consumption >= 0:
                total += consumption
            else:
                consumption = None

        result.append({
            "date": day.isoformat(),
            "start": v_start,
            "end": v_end,
            "consumption": consumption,
        })

        day += timedelta(days=1)

    con.close()
    return result, total


@app.route("/")
def index():
    sensors = get_energy_sensors()
    selected = request.args.get("entity", DEFAULT_ENTITY)
    return render_template("index.html", sensors=sensors, selected=selected,
                           result=None, total=None, error=None, form=request.args)


@app.route("/calculate", methods=["GET", "POST"])
def calc():
    sensors = get_energy_sensors()
    data = request.form if request.method == "POST" else request.args

    entity = data.get("entity", DEFAULT_ENTITY)
    start_s = data.get("start", "")
    end_s = data.get("end", "")
    from_s = data.get("from_time", "19:00")
    to_s = data.get("to_time", "23:30")

    try:
        start_date = datetime.strptime(start_s, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_s, "%Y-%m-%d").date()
        start_time = datetime.strptime(from_s, "%H:%M").time()
        end_time = datetime.strptime(to_s, "%H:%M").time()

        if end_date < start_date:
            raise ValueError("Das Enddatum muss nach dem Startdatum liegen.")

        result, total = calculate(entity, start_date, end_date, start_time, end_time)
        valid_days = sum(r["consumption"] is not None for r in result)

        return render_template("index.html", sensors=sensors, selected=entity,
                               result=result, total=total, valid_days=valid_days,
                               error=None, form=data)
    except Exception as exc:
        return render_template("index.html", sensors=sensors, selected=entity,
                               result=None, total=None, error=str(exc), form=data), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
