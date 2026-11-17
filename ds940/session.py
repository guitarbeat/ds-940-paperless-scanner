"""Session and power-management lifecycle for the scanner receiver."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time


class SessionManager:
    """Manages active scanning sessions, idle countdowns, and battery-saver dormancy."""

    def __init__(self, work_dir: Path, idle_timeout: float = 180.0):
        self.work_dir = work_dir
        self.idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._session_deadline: float = time.monotonic() + idle_timeout
        self._cmd_file = work_dir / "session-command.json"

    def get_remaining(self) -> float:
        with self._lock:
            self._check_file_command()
            return max(0.0, self._session_deadline - time.monotonic())

    def is_active(self) -> bool:
        return self.get_remaining() > 0.0

    def touch(self, duration: float | None = None) -> float:
        """Reset or extend the active session deadline."""
        with self._lock:
            add_sec = duration if duration is not None else self.idle_timeout
            self._session_deadline = time.monotonic() + add_sec
            return add_sec

    def extend(self, extra_seconds: float = 300.0) -> float:
        """Add time to the active session."""
        with self._lock:
            now = time.monotonic()
            current_base = max(now, self._session_deadline)
            self._session_deadline = current_base + extra_seconds
            return self._session_deadline - now

    def pause(self) -> None:
        """Immediately drop session deadline to 0 (enter dormant battery saver)."""
        with self._lock:
            self._session_deadline = 0.0

    def write_command(self, action: str, duration: float = 300.0) -> dict:
        """Write command and execute action."""
        with self._lock:
            res = self._handle_action_unlocked(action, duration)
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                payload = {"action": action, "duration": duration, "time": time.time()}
                tmp = self._cmd_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(self._cmd_file)
            except Exception:
                pass
            return res

    def _handle_action_unlocked(self, action: str, duration: float = 300.0) -> dict:
        if action == "wake":
            self._session_deadline = time.monotonic() + duration
            return {
                "status": "ok",
                "action": "wake",
                "session_active": True,
                "idle_remaining": round(duration, 1),
            }
        elif action == "extend":
            now = time.monotonic()
            base = max(now, self._session_deadline)
            self._session_deadline = base + duration
            remaining = self._session_deadline - now
            return {
                "status": "ok",
                "action": "extend",
                "session_active": True,
                "idle_remaining": round(remaining, 1),
            }
        elif action == "pause":
            self._session_deadline = 0.0
            return {
                "status": "ok",
                "action": "pause",
                "session_active": False,
                "idle_remaining": 0.0,
            }
        return {"status": "error", "message": f"Unknown action: {action}"}

    def _check_file_command(self) -> None:
        if not self._cmd_file.exists():
            return
        try:
            data = json.loads(self._cmd_file.read_text(encoding="utf-8"))
            self._cmd_file.unlink(missing_ok=True)
            action = data.get("action")
            duration = float(data.get("duration", 300.0))
            if action == "wake":
                self._session_deadline = time.monotonic() + duration
            elif action == "extend":
                self._session_deadline = max(time.monotonic(), self._session_deadline) + duration
            elif action == "pause":
                self._session_deadline = 0.0
        except Exception:
            pass
