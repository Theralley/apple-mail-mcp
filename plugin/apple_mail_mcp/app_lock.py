"""Cross-process lock so only one client drives an Apple app at a time.

Mail answers Apple Events serially, so several MCP servers (one per Claude,
Codex or agy session) calling it at once just queue inside Mail until the
slowest call times out. This lock makes them queue outside Mail instead, with
a clear "busy" error once the wait budget is spent.

Protocol, shared with the apple-mcp server so the two exclude each other:
``~/Library/Caches/apple-app-locks/<App>.lock`` is a directory created with
mkdir (atomic). Its ``owner`` file holds ``<pid> <epoch-ms>``. A lock whose
owner process is gone, or which is older than ``STALE_AFTER_S``, is stale and
may be taken over.
"""

import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path(os.environ.get("APPLE_APP_LOCK_DIR", Path.home() / "Library/Caches/apple-app-locks"))
STALE_AFTER_S = 300.0
POLL_S = 0.2


class AppBusyError(Exception):
    pass


def _wait_budget_s() -> float:
    try:
        return float(os.environ.get("APPLE_APP_LOCK_WAIT_MS", "30000")) / 1000.0
    except ValueError:
        return 30.0


def _is_stale(lock: Path) -> bool:
    try:
        pid_s, ms_s = (lock / "owner").read_text().split()
        pid, started = int(pid_s), int(ms_s) / 1000.0
    except (OSError, ValueError):
        # Owner file not written yet: only stale once the directory is old.
        try:
            return time.time() - lock.stat().st_mtime > 5.0
        except OSError:
            return False
    if time.time() - started > STALE_AFTER_S:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _remove(lock: Path) -> None:
    try:
        (lock / "owner").unlink()
    except OSError:
        pass
    try:
        lock.rmdir()
    except OSError:
        pass


@contextmanager
def app_lock(app: str, wait_s=None):
    """Hold the lock for *app* for the duration of the block."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{app}.lock"
    deadline = time.monotonic() + (_wait_budget_s() if wait_s is None else wait_s)
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if _is_stale(lock):
                _remove(lock)
                continue
            if time.monotonic() >= deadline:
                raise AppBusyError(
                    f"{app} is busy with a request from another session. "
                    "Try again in a moment."
                )
            time.sleep(POLL_S)
    try:
        (lock / "owner").write_text(f"{os.getpid()} {int(time.time() * 1000)}")
        yield
    finally:
        _remove(lock)
