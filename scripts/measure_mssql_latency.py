#!/usr/bin/env python3
"""Phase 0.1 - measure SQL Server latency FROM THE SERVER.

The whole migration estimate turns on this number. Run it on the AWS box:

    venv/bin/python scripts/measure_mssql_latency.py
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONNECTION_STRING = os.getenv("MSSQL_CONNECTION_STRING", "")


def normalise(value: str) -> str:
    if "driver=" in value.lower():
        return value
    return "DRIVER={ODBC Driver 18 for SQL Server};" + value


def main() -> int:
    if not CONNECTION_STRING:
        print("Set MSSQL_CONNECTION_STRING (it is already in .env).")
        return 1
    try:
        import pyodbc
    except ImportError:
        print("pip install pyodbc, and apt-get install msodbcsql18")
        return 1

    started = time.time()
    conn = pyodbc.connect(normalise(CONNECTION_STRING), timeout=60)
    connect_ms = (time.time() - started) * 1000
    cursor = conn.cursor()

    timings = []
    for _ in range(15):
        started = time.time()
        cursor.execute("SELECT COUNT(*) FROM recruiter_applications")
        cursor.fetchone()
        timings.append((time.time() - started) * 1000)

    median = statistics.median(timings)
    print(f"connect          : {connect_ms:8.0f} ms")
    print(f"query median     : {median:8.1f} ms   (min {min(timings):.1f}, max {max(timings):.1f})")
    print(f"6 lookups        : {median * 6 / 1000:8.1f} s    - a small part of one email")
    print(f"30 lookups       : {median * 30 / 1000:8.1f} s    - closer to a full email")
    print()
    if median < 15:
        print("VERDICT: fine. Latency is not an obstacle; treat this as a normal port.")
    elif median < 60:
        print("VERDICT: workable, but add connection pooling and batch the interview path.")
    else:
        print("VERDICT: too slow for this workload as written. The agent would need")
        print("         its queries restructured, or the database moved closer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
