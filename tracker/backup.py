"""Backups of the tracker database. It runs in WAL mode, so a plain file copy can miss recent rows
or catch a half-written page; SQLite's online backup API copies a consistent snapshot while the bot runs."""
import sqlite3
from datetime import datetime
from pathlib import Path


def backup_database(db_path, to_dir, keep: int = 14, now: datetime | None = None) -> Path:
    """Copy db_path into to_dir as <name>-YYYY-MM-DD-HHMM.db; keep the newest `keep` copies of it."""
    db_path, to_dir = Path(db_path), Path(to_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")
    to_dir.mkdir(parents=True, exist_ok=True)
    out = to_dir / f"{db_path.stem}-{(now or datetime.now()):%Y-%m-%d-%H%M}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    copies = sorted(to_dir.glob(f"{db_path.stem}-????-??-??-????.db"))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    return out
