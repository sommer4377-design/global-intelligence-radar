"""本地正式简报的到期判定与 SQLite 记录。"""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile, TemporaryDirectory

from radar import BEIJING

LOOKBACK = timedelta(hours=48)


def due_slots(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    local = now.astimezone(BEIJING)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in (-2, -1, 0):
        for hour in (8, 20):
            slot = midnight + timedelta(days=offset, hours=hour)
            if local - LOOKBACK <= slot <= local:
                yield slot


def write_if_changed(path, html):
    if path.is_file() and path.read_text(encoding="utf-8") == html:
        return
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as handle:
        pending = Path(handle.name)
        try:
            handle.write(html)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def generate_due(database, output, now, render):
    """Save HTML and a unique slot key together; recover files from committed state."""
    output.mkdir(parents=True, exist_ok=True)
    completed = []
    with closing(sqlite3.connect(database, timeout=120)) as db, TemporaryDirectory() as scratch:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS editions("
                       "slot TEXT PRIMARY KEY, generated_at TEXT NOT NULL, html TEXT NOT NULL)")
        for slot in due_slots(now):
            key = slot.isoformat()
            name = slot.strftime("brief-%Y-%m-%d-%H.html")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM editions WHERE slot = ?", (key,)).fetchone():
                    continue
                target = Path(scratch) / name
                render(slot, target)
                db.execute("INSERT INTO editions VALUES (?, ?, ?)",
                           (key, now.isoformat(), target.read_text(encoding="utf-8")))
                completed.append(key)
        for key, html in db.execute("SELECT slot, html FROM editions ORDER BY slot"):
            name = datetime.fromisoformat(key).strftime("brief-%Y-%m-%d-%H.html")
            write_if_changed(output / name, html)
    return completed
