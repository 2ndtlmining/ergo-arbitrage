"""One bot per database: an exclusive OS lock on <database>.lock.

Two bots on one wallet would each keep their own daily cap, cooldown and drawdown baseline (every
limit doubled) and race for the same boxes; a manual `arb.py ... --execute` during a live trade can
spend leg 1's SigUSD. The OS releases the lock when the process dies, so a crash never leaves a
stale lock. Who holds it (pid, mode, since) is written to <database>.lock.json for the error message.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path


class LockHeld(Exception):
    def __init__(self, holder: dict):
        self.holder = holder
        who = (f"pid {holder.get('pid', '?')}, mode {holder.get('mode', '?')}, since {holder.get('since', '?')}"
               if holder else "holder unknown")
        super().__init__(f"another bot is already running on this database ({who})")


def _try_lock(fh):
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh):
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    def __init__(self, db_path, mode: str):
        self.path = Path(f"{db_path}.lock")
        self.info = Path(f"{db_path}.lock.json")
        self.mode = mode
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            _try_lock(fh)
        except OSError:
            fh.close()
            raise LockHeld(_read_info(self.info)) from None
        self._fh = fh
        self.info.write_text(json.dumps({"pid": os.getpid(), "mode": self.mode,
                                         "since": datetime.now().isoformat(timespec="seconds")}))
        return self

    def release(self):
        if self._fh is None:
            return
        try:
            self.info.unlink(missing_ok=True)
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def _read_info(info: Path) -> dict:
    try:
        return json.loads(info.read_text())
    except (OSError, ValueError):
        return {}


def holder(db_path):
    """Who holds the lock (dict, possibly empty), or None when it is free."""
    probe = InstanceLock(db_path, "probe")
    try:
        probe.acquire()
    except LockHeld as e:
        return e.holder
    probe.release()
    return None
