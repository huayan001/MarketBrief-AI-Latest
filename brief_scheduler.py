"""Small in-process scheduler for the first paid daily-brief MVP."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db


class BriefScheduler:
    def __init__(self, runner: Callable[[dict], None], poll_seconds: int = 30) -> None:
        self.runner = runner
        self.poll_seconds = max(10, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="brief-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        for preferences in db.list_enabled_brief_preferences():
            try:
                local_now = now.astimezone(ZoneInfo(preferences["timezone"]))
            except ZoneInfoNotFoundError:
                continue
            # Run any brief whose delivery time has passed. The unique daily job
            # constraint makes this safe and lets a restarted worker catch up.
            if local_now.strftime("%H:%M") < preferences["delivery_time"]:
                continue
            payload = {**preferences, "local_date": local_now.date().isoformat()}
            self.runner(payload)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.run_once()
            except Exception as exc:
                print(f"[brief-scheduler] tick failed: {exc}")
