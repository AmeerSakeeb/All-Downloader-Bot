"""Container health signal: recent app heartbeat plus readable SQLite database."""

import json
import sqlite3
import sys
import time
from core.config import Settings


def is_healthy(settings: Settings) -> bool:
    """Read configured paths without creating a database or contacting Telegram."""
    try:
        state = json.loads((settings.data_dir / "health.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return False
        age = time.time() - float(state["timestamp"])
        if not 0 <= age <= 90:
            return False
        if state.get("database_ok") is not True or state.get("scheduler_alive") is not True:
            return False
        if any(state.get(key) is not True for key in ("proxy_alive", "maintenance_alive", "maintenance_ok")):
            return False
        database_uri = settings.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=2) as connection:
            connection.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
        return True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


if __name__ == "__main__":
    try:
        sys.exit(0 if is_healthy(Settings()) else 1)
    except Exception:
        sys.exit(1)
