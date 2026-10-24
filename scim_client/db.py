import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def connect(path=None):
    path = Path(path or os.environ.get("SCIM_DB", ROOT / "data/client.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not path.exists()
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript((ROOT / "sql/schema.sql").read_text())
    if fresh:
        db.executescript((ROOT / "sql/seed.sql").read_text())
    return db
