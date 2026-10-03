"""`arb.py backup`: a consistent copy of the WAL-mode tracker database, newest N kept (#86)."""
import sqlite3
from datetime import datetime

import pytest

from tracker.backup import backup_database


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1), (2), (3)")
    conn.commit()
    return conn                                  # left open: the rows may still sit in the WAL file


def rows(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT count(*) FROM t").fetchone()[0]


def test_backup_includes_rows_still_in_the_wal(tmp_path):
    db = tmp_path / "arbitrage_tracker.db"
    live = make_db(db)
    out = backup_database(db, tmp_path / "backups", now=datetime(2026, 10, 4, 3, 0))
    live.close()
    assert out.name == "arbitrage_tracker-2026-10-04-0300.db"
    assert rows(out) == 3


def test_only_the_newest_copies_are_kept(tmp_path):
    db = tmp_path / "arbitrage_tracker.db"
    make_db(db).close()
    for day in range(1, 6):
        backup_database(db, tmp_path / "b", keep=3, now=datetime(2026, 10, day))
    names = sorted(p.name for p in (tmp_path / "b").iterdir())
    assert names == [f"arbitrage_tracker-2026-10-0{d}-0000.db" for d in (3, 4, 5)]


def test_unrelated_files_in_the_folder_are_never_deleted(tmp_path):
    db = tmp_path / "arbitrage_tracker.db"
    make_db(db).close()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "notes.txt").write_text("keep me")
    backup_database(db, tmp_path / "b", keep=1, now=datetime(2026, 10, 1))
    backup_database(db, tmp_path / "b", keep=1, now=datetime(2026, 10, 2))
    assert (tmp_path / "b" / "notes.txt").exists()


def test_missing_database_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "nope.db", tmp_path / "b")


def test_cli_backup(tmp_path, capsys):
    import arb
    db = tmp_path / "arbitrage_tracker.db"
    make_db(db).close()
    arb.main(["backup", "--db", str(db), "--to", str(tmp_path / "b"), "--keep", "2"])
    out = capsys.readouterr().out
    assert "arbitrage_tracker-" in out and len(list((tmp_path / "b").iterdir())) == 1
