from flask import Flask, render_template, request
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)

DB = "/config/home-assistant_v2.db"
LOCAL_TZ = ZoneInfo("Europe/Berlin")


def get_connection():
    if not os.path.exists(DB):
        raise FileNotFoundError(
            f"Recorder-Datenbank nicht gefunden: {DB}"
        )

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def get_energy_sensors():
    """
    Liefert alle Sensoren aus statistics_meta,
    die als kWh-Sensor geführt werden.
    """

    con = get_connection()

    try:
        rows = con.execute("""
            SELECT
                id,
                statistic_id,
                unit_of_measurement,
                name
            FROM statistics_meta
            WHERE unit_of_measurement = 'kWh'
            ORDER BY statistic_id
        """).fetchall()

        return [
            {
                "id": row["id"],
                "statistic_id": row["statistic_id"],
                "name": row["name"] or row["statistic_id"]
            }
            for row in rows
        ]

    finally:
        con.close()


def local_timestamp(dt):
    """
    Wandelt eine lokale Europe/Berlin-Zeit
    in Unix-Timestamp um.
    """

    return dt.replace(tzinfo=LOCAL_TZ).timestamp()


def get_statistic_value(con, metadata_id, dt, direction="before"):
    """
    Holt einen Statistikwert.

    direction="before":
        letzter vorhandener Wert bei oder vor dt

    direction="after":
        erster vorhandener Wert bei oder nach dt
    """

    ts = local_timestamp(dt)

    if direction == "before":

        row = con.execute("""
            SELECT
                start_ts,
                state,
                sum
            FROM statistics
            WHERE metadata_id = ?
              AND start_ts <= ?
              AND state IS NOT NULL
            ORDER BY start_ts DESC
            LIMIT 1
        """, (metadata_id, ts)).fetchone()

    else:

        row = con.execute("""
            SELECT
                start_ts,
                state,
                sum
            FROM statistics
            WHERE metadata_id = ?
              AND start_ts >= ?
              AND state IS NOT NULL
            ORDER BY start_ts ASC
            LIMIT 1
        """, (metadata_id, ts)).fetchone()

    if row is None:
        return None

    # Für einen kumulativen kWh-Zähler verwenden wir state.
    value = row["state"]

    if value is None:
        value = row["sum"]

    if value is None:
        return None

    return {
        "timestamp": row["start_ts"],
        "value": float(value)
    }


def calculate_day(con, metadata_id, date_value, from_time, to_time):
    """
    Berechnet den Verbrauch für einen einzelnen Tag.

    Beispiel:
        19:00 -> 00:00

    Das Ende 00:00 liegt am folgenden Kalendertag.
    """

    start_dt = datetime.combine(
        date_value,
        from_time
    )

    if to_time == time(0, 0):
        end_date = date_value + timedelta(days=1)
        end_dt = datetime.combine(
            end_date,
            to_time
        )
    elif to_time <= from_time:
        end_date = date_value + timedelta(days=1)
        end_dt = datetime.combine(
            end_date,
            to_time
        )
    else:
        end_dt = datetime.combine(
            date_value,
            to_time
        )

    start = get_statistic_value(
        con,
        metadata_id,
        start_dt,
        direction="before"
    )

    end = get_statistic_value(
        con,
        metadata_id,
        end_dt,
        direction="before"
    )

    if start is None or end is None:
        return {
            "date": date_value.strftime("%d.%m.%Y"),
            "start": None,
            "end": None,
            "consumption": None
        }

    consumption = end["value"] - start["value"]

    # Ein negativer Wert deutet normalerweise
    # auf einen Zählerreset oder fehlerhafte Daten hin.
    if consumption < 0:
        return {
            "date": date_value.strftime("%d.%m.%Y"),
            "start": start["value"],
            "end": end["value"],
            "consumption": None
        }

    return {
        "date": date_value.strftime("%d.%m.%Y"),
        "start": start["value"],
        "end": end["value"],
        "consumption": consumption
    }


@app.route("/", methods=["GET"])
def index():

    try:
        sensors = get_energy_sensors()

        selected = (
            request.args.get("entity")
            or (
                sensors[0]["statistic_id"]
                if sensors
                else None
            )
        )

        return render_template(
            "index.html",
            sensors=sensors,
            selected=selected,
            total=None,
            valid_days=0,
            missing_days=0,
            average=None,
            result=[],
            error=None,
            form={}
        )

    except Exception as e:

        return render_template(
            "index.html",
            sensors=[],
            selected=None,
            total=None,
            valid_days=0,
            missing_days=0,
            average=None,
            result=[],
            error=str(e),
            form={}
        )


@app.route("/calculate", methods=["POST"])
def calc():

    form = request.form

    entity = form.get("entity")

    start_string = form.get("start")
    end_string = form.get("end")

    from_time_string = form.get("from_time", "19:00")
    to_time_string = form.get("to_time", "00:00")

    try:

        start_date = datetime.strptime(
            start_string,
            "%Y-%m-%d"
        ).date()

        end_date = datetime.strptime(
            end_string,
            "%Y-%m-%d"
        ).date()

        from_time = datetime.strptime(
            from_time_string,
            "%H:%M"
        ).time()

        to_time = datetime.strptime(
            to_time_string,
            "%H:%M"
        ).time()

        if end_date < start_date:
            raise ValueError(
                "Das Enddatum darf nicht vor dem Startdatum liegen."
            )

        sensors = get_energy_sensors()

        sensor = next(
            (
                s
                for s in sensors
                if s["statistic_id"] == entity
            ),
            None
        )

        if sensor is None:
            raise ValueError(
                "Der ausgewählte Energy-Sensor wurde "
                "nicht in statistics_meta gefunden."
            )

        metadata_id = sensor["id"]

        con = get_connection()

        try:

            result = []

            current_date = start_date

            while current_date <= end_date:

                day_result = calculate_day(
                    con,
                    metadata_id,
                    current_date,
                    from_time,
                    to_time
                )

                result.append(day_result)

                current_date += timedelta(days=1)

        finally:
            con.close()

        valid = [
            r
            for r in result
            if r["consumption"] is not None
        ]

        missing = [
            r
            for r in result
            if r["consumption"] is None
        ]

        total = sum(
            r["consumption"]
            for r in valid
        )

        average = (
            total / len(valid)
            if valid
            else None
        )

        return render_template(
            "index.html",
            sensors=sensors,
            selected=entity,
            total=total,
            valid_days=len(valid),
            missing_days=len(missing),
            average=average,
            result=result,
            error=None,
            form=form
        )

    except Exception as e:

        sensors = []

        try:
            sensors = get_energy_sensors()
        except Exception:
            pass

        return render_template(
            "index.html",
            sensors=sensors,
            selected=entity,
            total=None,
            valid_days=0,
            missing_days=0,
            average=None,
            result=[],
            error=str(e),
            form=form
        )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8099
    )
