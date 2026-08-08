#!/usr/bin/env python3
"""Independent daily-brief worker for production deployments."""

from __future__ import annotations

import os
import signal
import threading

import brief_scheduler
import db
import server


def main() -> None:
    server.load_env_file()
    db.ensure_schema()
    scheduler = brief_scheduler.BriefScheduler(
        server.run_daily_brief,
        poll_seconds=int(os.environ.get("BRIEF_POLL_SECONDS", "30")),
    )
    if "--once" in __import__("sys").argv:
        scheduler.run_once()
        return
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    print("MarketBrief AI brief worker started")
    while not stopped.wait(30):
        scheduler.run_once()


if __name__ == "__main__":
    main()
