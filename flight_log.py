"""
flight_log.py
=============
FAA Part 107 Flight Log for professional hour tracking.

Logs every flight automatically when integrated with Safety1271,
or manually for park flights, photography shoots, AMA field sessions,
and any other drone operations.

Use cases:
  - LinkedIn profile: "X hours of autonomous drone operations"
  - Client proposals: demonstrate experience level
  - Insurance claims: documented flight history
  - Maintenance scheduling: prop cycles, battery cycles
  - Part 107 currency: verify recent flight activity

Usage:
  # Add a manual flight entry
  python flight_log.py --add

  # Show total hours summary
  python flight_log.py --summary

  # Show recent flights
  python flight_log.py --recent 10

  # Export for LinkedIn / resume
  python flight_log.py --profile

  # Export to CSV
  python flight_log.py --export flights.csv

  # Query specific aircraft or type
  python flight_log.py --summary --aircraft "DJI Mini 4 Pro"

Integration with Safety1271 (run_safety1271.py):
  from flight_log import FlightLogger
  logger = FlightLogger()
  flight_id = logger.start_flight(aircraft="DJI Mini 4 Pro",
                                   location="Burke Community Church",
                                   operation_type="patrol")
  # ... patrol runs ...
  logger.end_flight(flight_id, notes="12 zones clear, 1 safety flag")
"""

import os, sqlite3, csv, argparse
from datetime import datetime, timezone
from pathlib  import Path
from typing   import Optional

LOG_PATH = Path(os.getenv("FLIGHT_LOG_PATH", "./flight_log.db"))

# Operation types
OPERATION_TYPES = {
    "patrol":       "Autonomous Security Patrol",
    "photography":  "Aerial Photography / Videography",
    "inspection":   "Infrastructure / Facility Inspection",
    "testing":      "Development & Testing",
    "demo":         "Demonstration",
    "recreation":   "Recreation / AMA Field",
    "training":     "Pilot Training",
    "survey":       "Mapping / Survey",
}

AIRCRAFT_LIST = [
    "DJI Mini 4 Pro",
    "DJI Air 3",
    "Other",
]


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(LOG_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS flights (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        date             TEXT NOT NULL,           -- YYYY-MM-DD
        start_time       TEXT NOT NULL,           -- HH:MM local
        end_time         TEXT,                    -- HH:MM local (null if in progress)
        duration_min     REAL,                    -- calculated on end_flight()
        aircraft         TEXT NOT NULL,
        location         TEXT NOT NULL,
        operation_type   TEXT NOT NULL,           -- patrol, photography, etc.
        conditions       TEXT,                    -- weather, visibility, wind notes
        notes            TEXT,
        auto_logged      INTEGER DEFAULT 0,       -- 1 if logged by Safety1271
        created_at       TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS batteries (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        flight_id    INTEGER REFERENCES flights(id),
        aircraft     TEXT,
        battery_id   TEXT,    -- serial or label e.g. "B1", "B2"
        start_pct    INTEGER,
        end_pct      INTEGER,
        cycles       INTEGER DEFAULT 1
    );

    CREATE VIEW IF NOT EXISTS flight_totals AS
        SELECT
            aircraft,
            operation_type,
            COUNT(*)                           AS flights,
            ROUND(SUM(duration_min) / 60.0, 2) AS hours,
            ROUND(AVG(duration_min), 1)        AS avg_min,
            MIN(date)                          AS first_flight,
            MAX(date)                          AS last_flight
        FROM flights
        WHERE duration_min IS NOT NULL
        GROUP BY aircraft, operation_type;
    """)
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  FLIGHT LOGGER CLASS  (used by Safety1271 integration)
# ══════════════════════════════════════════════════════════════════════════════

class FlightLogger:
    """
    Use this class to auto-log flights from Safety1271 or any other script.

    Example:
        logger = FlightLogger()
        fid = logger.start_flight(
            aircraft="DJI Mini 4 Pro",
            location="Burke Community Church, Springfield VA",
            operation_type="patrol"
        )
        # ... do the patrol ...
        logger.end_flight(fid, notes="All clear. 1 vehicle flagged.")
    """

    def __init__(self):
        self.conn = get_db()
        init_db(self.conn)

    def start_flight(self,
                     aircraft: str,
                     location: str,
                     operation_type: str = "patrol",
                     conditions: str = "",
                     notes: str = "") -> int:
        """
        Log flight start. Returns flight_id for use with end_flight().
        Call this immediately after takeoff confirmation.
        """
        now   = datetime.now()
        date  = now.strftime("%Y-%m-%d")
        start = now.strftime("%H:%M")

        cur = self.conn.execute(
            """INSERT INTO flights
               (date, start_time, aircraft, location, operation_type,
                conditions, notes, auto_logged)
               VALUES (?,?,?,?,?,?,?,1)""",
            (date, start, aircraft, location, operation_type, conditions, notes)
        )
        self.conn.commit()
        flight_id = cur.lastrowid
        print(f"  ✈️  Flight #{flight_id} started — {aircraft} at {location} ({start})")
        return flight_id

    def end_flight(self, flight_id: int,
                   notes: str = "",
                   battery_end_pct: Optional[int] = None) -> float:
        """
        Log flight end. Calculates duration and returns hours flown.
        Call this immediately after landing confirmation.
        """
        row = self.conn.execute(
            "SELECT start_time, date FROM flights WHERE id=?", (flight_id,)
        ).fetchone()

        if not row:
            print(f"  ⚠️  Flight #{flight_id} not found in log")
            return 0.0

        now      = datetime.now()
        end_time = now.strftime("%H:%M")

        # Calculate duration
        start_dt = datetime.strptime(f"{row['date']} {row['start_time']}", "%Y-%m-%d %H:%M")
        duration_min = (now - start_dt).total_seconds() / 60.0

        self.conn.execute(
            """UPDATE flights SET end_time=?, duration_min=?,
               notes = CASE WHEN notes='' THEN ? ELSE notes || ' | ' || ? END
               WHERE id=?""",
            (end_time, round(duration_min, 1), notes, notes, flight_id)
        )
        self.conn.commit()

        hours = duration_min / 60.0
        print(f"  🛬  Flight #{flight_id} ended — "
              f"{duration_min:.0f} min ({hours:.2f} hrs) logged")
        return hours

    def total_hours(self, aircraft: str = None) -> float:
        """Return total hours flown, optionally filtered by aircraft."""
        if aircraft:
            row = self.conn.execute(
                "SELECT SUM(duration_min) FROM flights WHERE duration_min IS NOT NULL AND aircraft=?",
                (aircraft,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT SUM(duration_min) FROM flights WHERE duration_min IS NOT NULL"
            ).fetchone()
        total_min = row[0] or 0
        return round(total_min / 60.0, 2)

    def close(self):
        self.conn.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MANUAL ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def add_manual_entry(conn):
    """Interactive CLI for adding a manual flight entry."""
    print("\n  ── Add Flight Entry ──────────────────────────────────")

    # Date
    date_in = input("  Date (YYYY-MM-DD) [today]: ").strip()
    if not date_in:
        date_in = datetime.now().strftime("%Y-%m-%d")

    # Start time
    start_in = input("  Start time (HH:MM) [now]: ").strip()
    if not start_in:
        start_in = datetime.now().strftime("%H:%M")

    # Duration
    dur_in = input("  Duration (minutes): ").strip()
    duration_min = float(dur_in) if dur_in else 0.0

    # End time (calculated)
    from datetime import timedelta
    try:
        start_dt = datetime.strptime(f"{date_in} {start_in}", "%Y-%m-%d %H:%M")
        end_dt   = start_dt + timedelta(minutes=duration_min)
        end_time = end_dt.strftime("%H:%M")
    except Exception:
        end_time = ""

    # Aircraft
    print("  Aircraft:")
    for i, a in enumerate(AIRCRAFT_LIST, 1):
        print(f"    {i}. {a}")
    ac_in = input("  Choice [1]: ").strip()
    aircraft = AIRCRAFT_LIST[int(ac_in)-1] if ac_in.isdigit() else AIRCRAFT_LIST[0]

    # Location
    location = input("  Location: ").strip() or "Unknown"

    # Operation type
    print("  Operation type:")
    types = list(OPERATION_TYPES.items())
    for i, (k, v) in enumerate(types, 1):
        print(f"    {i}. {v}")
    op_in = input("  Choice [1]: ").strip()
    op_type = types[int(op_in)-1][0] if op_in.isdigit() else "patrol"

    # Conditions
    conditions = input("  Conditions (weather, wind, visibility): ").strip()

    # Notes
    notes = input("  Notes: ").strip()

    conn.execute(
        """INSERT INTO flights
           (date, start_time, end_time, duration_min, aircraft,
            location, operation_type, conditions, notes, auto_logged)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (date_in, start_in, end_time, duration_min, aircraft,
         location, op_type, conditions, notes)
    )
    conn.commit()
    print(f"\n  ✅  Flight logged: {duration_min:.0f} min "
          f"({duration_min/60:.2f} hrs) on {date_in}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  SUMMARY AND REPORTING
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(conn, aircraft_filter: str = None):
    """Print total hours summary by operation type."""
    where = f"WHERE aircraft='{aircraft_filter}'" if aircraft_filter else ""

    rows = conn.execute(f"""
        SELECT operation_type, aircraft,
               COUNT(*) flights,
               ROUND(SUM(duration_min)/60.0, 2) hours,
               MIN(date) first, MAX(date) last
        FROM flights
        WHERE duration_min IS NOT NULL {('AND aircraft=?' if aircraft_filter else '')}
        GROUP BY operation_type, aircraft
        ORDER BY hours DESC
    """, (aircraft_filter,) if aircraft_filter else ()).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) flights, ROUND(SUM(duration_min)/60.0,2) hours "
        f"FROM flights WHERE duration_min IS NOT NULL "
        + (f"AND aircraft=?" if aircraft_filter else ""),
        (aircraft_filter,) if aircraft_filter else ()
    ).fetchone()

    print(f"\n  {'─'*55}")
    print(f"  FLIGHT LOG SUMMARY {'(' + aircraft_filter + ')' if aircraft_filter else ''}")
    print(f"  {'─'*55}")
    print(f"  {'Operation':<28} {'Aircraft':<18} {'Flights':>7} {'Hours':>7}")
    print(f"  {'─'*55}")

    for r in rows:
        label = OPERATION_TYPES.get(r['operation_type'], r['operation_type'])
        print(f"  {label:<28} {r['aircraft']:<18} {r['flights']:>7} {r['hours']:>7.2f}")

    print(f"  {'─'*55}")
    print(f"  {'TOTAL':<46} {total['flights']:>7} {total['hours'] or 0:>7.2f}")
    print(f"  {'─'*55}\n")


def print_recent(conn, n: int = 10):
    """Print N most recent flights."""
    rows = conn.execute(
        """SELECT id, date, start_time, end_time, duration_min,
                  aircraft, location, operation_type, notes, auto_logged
           FROM flights ORDER BY date DESC, start_time DESC LIMIT ?""",
        (n,)
    ).fetchall()

    print(f"\n  {'─'*70}")
    print(f"  RECENT FLIGHTS (last {n})")
    print(f"  {'─'*70}")
    for r in rows:
        auto = "🤖" if r['auto_logged'] else "✏️"
        dur  = f"{r['duration_min']:.0f}m" if r['duration_min'] else "in progress"
        label = OPERATION_TYPES.get(r['operation_type'], r['operation_type'])
        print(f"  {auto} #{r['id']:<4} {r['date']}  {r['start_time']}  "
              f"{dur:>6}  {r['aircraft']:<20} {label}")
        if r['notes']:
            print(f"          📝 {r['notes'][:70]}")
    print(f"  {'─'*70}\n")


def print_profile(conn):
    """
    Generate a formatted professional profile summary suitable for
    LinkedIn, resume, or client proposals.
    """
    total = conn.execute(
        "SELECT COUNT(*) flights, ROUND(SUM(duration_min)/60.0,2) hours "
        "FROM flights WHERE duration_min IS NOT NULL"
    ).fetchone()

    by_type = conn.execute(
        """SELECT operation_type,
                  ROUND(SUM(duration_min)/60.0,2) hours,
                  COUNT(*) flights
           FROM flights WHERE duration_min IS NOT NULL
           GROUP BY operation_type ORDER BY hours DESC"""
    ).fetchall()

    by_aircraft = conn.execute(
        """SELECT aircraft,
                  ROUND(SUM(duration_min)/60.0,2) hours,
                  COUNT(*) flights,
                  MIN(date) first_flight
           FROM flights WHERE duration_min IS NOT NULL
           GROUP BY aircraft"""
    ).fetchall()

    first = conn.execute(
        "SELECT MIN(date) FROM flights WHERE duration_min IS NOT NULL"
    ).fetchone()[0]

    last = conn.execute(
        "SELECT MAX(date) FROM flights WHERE duration_min IS NOT NULL"
    ).fetchone()[0]

    print("\n" + "═"*60)
    print("  PROFESSIONAL FLIGHT PROFILE")
    print("  Innocent Wafula Wanyama — FAA Part 107 Remote Pilot")
    print("═"*60)
    print(f"\n  Total flight experience: {total['hours'] or 0:.1f} hours "
          f"({total['flights']} flights)")
    print(f"  Flying since: {first}  ·  Most recent: {last}")
    print()
    print("  BY OPERATION TYPE:")
    for r in by_type:
        label = OPERATION_TYPES.get(r['operation_type'], r['operation_type'])
        bar   = "█" * int(r['hours'])
        print(f"    {label:<35} {r['hours']:>6.1f} hrs  {bar}")

    print()
    print("  BY AIRCRAFT:")
    for r in by_aircraft:
        print(f"    {r['aircraft']:<25} {r['hours']:>6.1f} hrs  "
              f"({r['flights']} flights, since {r['first_flight']})")

    print()
    print("  ── LINKEDIN / RESUME COPY ──────────────────────────────")
    lines = [
        f"FAA Part 107 Certified Remote Pilot | {total['hours'] or 0:.0f}+ flight hours",
        "",
    ]
    for r in by_type:
        label = OPERATION_TYPES.get(r['operation_type'], r['operation_type'])
        lines.append(f"• {label}: {r['hours']:.1f} hrs")

    lines.append("")
    for r in by_aircraft:
        lines.append(f"Aircraft: {r['aircraft']}")
    lines.append("Specialisation: AI-enhanced autonomous drone operations")
    lines.append("Location: Northern Virginia (DC SFRA / Class D experience)")

    for line in lines:
        print(f"  {line}")
    print("═"*60 + "\n")


def export_csv(conn, filepath: str):
    """Export all flights to CSV."""
    rows = conn.execute(
        """SELECT id, date, start_time, end_time, duration_min, aircraft,
                  location, operation_type, conditions, notes, auto_logged
           FROM flights ORDER BY date DESC, start_time DESC"""
    ).fetchall()

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["#", "Date", "Start", "End", "Duration (min)",
                         "Aircraft", "Location", "Operation", "Conditions",
                         "Notes", "Auto-logged"])
        for r in rows:
            writer.writerow([
                r["id"], r["date"], r["start_time"], r["end_time"] or "",
                r["duration_min"] or "", r["aircraft"], r["location"],
                OPERATION_TYPES.get(r["operation_type"], r["operation_type"]),
                r["conditions"] or "", r["notes"] or "",
                "Yes" if r["auto_logged"] else "No"
            ])
    print(f"\n  ✅  Exported {len(rows)} flights to {filepath}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  SAFETY1271 AUTO-INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def get_patrol_logger():
    """
    Returns a configured FlightLogger for use in Safety1271 patrol agents.
    Import and call this in run_safety1271.py:

        from flight_log import get_patrol_logger
        flight_logger = get_patrol_logger()
        flight_id = flight_logger.start_flight(
            aircraft=os.getenv("DRONE_MODEL", "DJI Mini 4 Pro"),
            location=os.getenv("COMPOUND_NAME", "Burke Community Church"),
            operation_type="patrol",
            conditions=f"Vis {preflight_vis}SM Wind {preflight_wind}kt"
        )
        # at end:
        flight_logger.end_flight(flight_id, notes=patrol_summary)
    """
    logger = FlightLogger()
    return logger


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Part 107 Flight Log — track hours for professional profile"
    )
    parser.add_argument("--add",      action="store_true", help="Add a manual flight entry")
    parser.add_argument("--summary",  action="store_true", help="Show hours summary by type")
    parser.add_argument("--recent",   type=int, metavar="N", help="Show N most recent flights")
    parser.add_argument("--profile",  action="store_true", help="Generate LinkedIn/resume profile")
    parser.add_argument("--export",   type=str, metavar="FILE", help="Export to CSV file")
    parser.add_argument("--aircraft", type=str, help="Filter by aircraft name")
    parser.add_argument("--total",    action="store_true", help="Print just the total hours")
    args = parser.parse_args()

    conn = get_db()
    init_db(conn)

    if args.add:
        add_manual_entry(conn)

    if args.summary or (not any(vars(args).values())):
        print_summary(conn, args.aircraft)

    if args.recent:
        print_recent(conn, args.recent)

    if args.profile:
        print_profile(conn)

    if args.export:
        export_csv(conn, args.export)

    if args.total:
        logger = FlightLogger()
        hrs = logger.total_hours(args.aircraft)
        print(f"\n  Total hours flown"
              f"{' (' + args.aircraft + ')' if args.aircraft else ''}: "
              f"{hrs:.2f}\n")

    conn.close()


if __name__ == "__main__":
    main()
