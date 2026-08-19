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
        raise FileNotFoundError(
            f"Recorder-Datenbank nicht gefunden: {DB}"
        )

    return sqlite3.connect(DB)


def get_energy_sensors():
    con = get_connection()
    cur = con.cursor()

    cur.execute("""
        SELECT DISTINCT sm.entity_id
        FROM states_meta sm
        JOIN states s
          ON s.metadata_id = sm.metadata_id
        WHERE sm.entity_id LIKE 'sensor.%'
          AND sm.entity_id LIKE '%energy%'
        ORDER BY sm.entity_id
    """)

    sensors = [row[0] for row in cur.fetchall()]

    con.close()

    return sensors


def get_metadata_id(cur, entity_id):
    cur.execute(
        """
        SELECT metadata_id
        FROM states_meta
        WHERE entity_id = ?
        """,
        (entity_id,)
    )

    row = cur.fetchone()

    if not row:
        raise ValueError(
            f"Sensor nicht im Recorder gefunden: {entity_id}"
        )

    return row[0]


def value_at(cur, metadata_id, dt):

    timestamp = dt.timestamp()

    cur.execute(
        """
        SELECT last_updated_ts, state
        FROM states
        WHERE metadata_id = ?
          AND state NOT IN ('unknown', 'unavailable')
          AND last_updated_ts <= ?
        ORDER BY last_updated_ts DESC
        LIMIT 1
        """,
        (metadata_id, timestamp)
    )

    before = cur.fetchone()

    cur.execute(
        """
        SELECT last_updated_ts, state
        FROM states
        WHERE metadata_id = ?
          AND state NOT IN ('unknown', 'unavailable')
          AND last_updated_ts >= ?
        ORDER BY last_updated_ts ASC
        LIMIT 1
        """,
        (metadata_id, timestamp)
    )

    after = cur.fetchone()

    if not before or not after:
        return None

    try:
        t1 = float(before[0])
        v1 = float(before[1])

        t2 = float(after[0])
        v2 = float(after[1])

    except (ValueError, TypeError):
        return None

    if t1 == t2:
        return v1

    ratio = (timestamp - t1) / (t2 - t1)

    return v1 + ratio * (v2 - v1)


def calculate(
    entity,
    start_date,
    end_date,
    start_time,
    end_time
):

    con = get_connection()
    cur = con.cursor()

    metadata_id = get_metadata_id(
        cur,
        entity
    )

    results = []

    total = 0.0

    day = start_date

    while day <= end_date:

        start_dt = datetime.combine(
            day,
            start_time,
            TZ
        )

        end_dt = datetime.combine(
            day,
            end_time,
            TZ
        )

        # Zeitfenster über Mitternacht
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        start_value = value_at(
            cur,
            metadata_id,
            start_dt
        )

        end_value = value_at(
            cur,
            metadata_id,
            end_dt
        )

        consumption = None

        if (
            start_value is not None
            and end_value is not None
        ):

            consumption = end_value - start_value

            # negativer Wert = wahrscheinlich Zählerreset
            if consumption < 0:
                consumption = None
            else:
                total += consumption

        results.append({
            "date": day.strftime("%d.%m.%Y"),
            "start": start_value,
            "end": end_value,
            "consumption": consumption
        })

        day += timedelta(days=1)

    con.close()

    return results, total


@app.route("/")
def index():

    sensors = get_energy_sensors()

    return render_template(
        "index.html",
        sensors=sensors,
        selected=DEFAULT_ENTITY,
        result=None,
        total=None,
        valid_days=0,
        error=None,
        form={}
    )


@app.route("/calculate", methods=["POST"])
def calculate_page():

    try:

        entity = request.form.get(
            "entity",
            DEFAULT_ENTITY
        )

        start_date = datetime.strptime(
            request.form["start"],
            "%Y-%m-%d"
        ).date()

        end_date = datetime.strptime(
            request.form["end"],
            "%Y-%m-%d"
        ).date()

        start_time = datetime.strptime(
            request.form["from_time"],
            "%H:%M"
        ).time()

        end_time = datetime.strptime(
            request.form["to_time"],
            "%H:%M"
        ).time()

        if end_date < start_date:
            raise ValueError(
                "Das Enddatum darf nicht vor dem Startdatum liegen."
            )

        results, total = calculate(
            entity,
            start_date,
            end_date,
            start_time,
            end_time
        )

        valid_days = sum(
            r["consumption"] is not None
            for r in results
        )

        sensors = get_energy_sensors()

        return render_template(
            "index.html",
            sensors=sensors,
            selected=entity,
            result=results,
            total=total,
            valid_days=valid_days,
            error=None,
            form=request.form
        )

    except Exception as error:

        sensors = get_energy_sensors()

        return render_template(
            "index.html",
            sensors=sensors,
            selected=request.form.get(
                "entity",
                DEFAULT_ENTITY
            ),
            result=None,
            total=None,
            valid_days=0,
            error=str(error),
            form=request.form
        )


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=8099
    )
