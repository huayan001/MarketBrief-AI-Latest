#!/usr/bin/env python3
"""One-time migration: import local SQLite reports into MySQL for a given user email."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env_file() -> None:
    for env_path in (ROOT / "api_keys.env", ROOT / ".env"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite reports into MySQL")
    parser.add_argument("--email", required=True, help="Target user email (created if missing)")
    parser.add_argument(
        "--sqlite",
        default=str(ROOT / "data" / "reports.sqlite3"),
        help="Path to old SQLite database",
    )
    args = parser.parse_args()

    load_env_file()

    from auth import normalize_email, upsert_user
    from db import ensure_schema, get_conn, utc_now

    ensure_schema()
    email = normalize_email(args.email)
    user = upsert_user(email)
    user_id = int(user["id"])

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite file not found: {sqlite_path}")

    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT symbol, asset_type, stance, summary, report_json, created_at
            FROM reports
            ORDER BY id ASC
            """
        ).fetchall()

    imported = 0
    with get_conn() as mysql:
        with mysql.cursor() as cur:
            for row in rows:
                created_at = row["created_at"]
                if isinstance(created_at, str):
                    created_value = created_at.replace("Z", "+00:00")
                    try:
                        created_at = datetime.fromisoformat(created_value).replace(tzinfo=None)
                    except ValueError:
                        created_at = utc_now()
                report_json = row["report_json"]
                if not isinstance(report_json, str):
                    report_json = json.dumps(report_json, ensure_ascii=False)
                cur.execute(
                    """
                    INSERT INTO reports(user_id, symbol, asset_type, stance, summary, report_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        row["symbol"],
                        row["asset_type"],
                        row["stance"],
                        row["summary"],
                        report_json,
                        created_at,
                    ),
                )
                imported += 1

    print(f"Imported {imported} reports for {email} (user_id={user_id})")


if __name__ == "__main__":
    main()
