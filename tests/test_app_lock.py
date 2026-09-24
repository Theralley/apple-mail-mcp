import multiprocessing as mp
import os
import subprocess
import time

import pytest

from apple_mail_mcp import app_lock as al


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "LOCK_DIR", tmp_path)
    return tmp_path


def _hold(lock_dir, seconds, ready):
    al.LOCK_DIR = lock_dir
    with al.app_lock("Mail"):
        ready.set()
        time.sleep(seconds)


def test_second_process_waits_for_the_first(lock_dir):
    ready = mp.Event()
    p = mp.Process(target=_hold, args=(lock_dir, 1.0, ready))
    p.start()
    assert ready.wait(10)
    t = time.monotonic()
    with al.app_lock("Mail", wait_s=10):
        waited = time.monotonic() - t
    p.join()
    assert waited > 0.5


def test_busy_error_when_wait_budget_spent(lock_dir):
    ready = mp.Event()
    p = mp.Process(target=_hold, args=(lock_dir, 2.0, ready))
    p.start()
    assert ready.wait(10)
    with pytest.raises(al.AppBusyError, match="Mail is busy"):
        with al.app_lock("Mail", wait_s=0.3):
            pass
    p.join()


def test_lock_of_a_dead_process_is_taken_over(lock_dir):
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = lock_dir / "Mail.lock"
    lock.mkdir()
    (lock / "owner").write_text(f"{dead.pid} {int(time.time() * 1000)}")
    with al.app_lock("Mail", wait_s=0.5):
        assert (lock / "owner").read_text().split()[0] == str(os.getpid())
    assert not lock.exists()


def test_old_lock_is_taken_over_even_if_owner_lives(lock_dir):
    lock = lock_dir / "Mail.lock"
    lock.mkdir()
    old = int((time.time() - al.STALE_AFTER_S - 1) * 1000)
    (lock / "owner").write_text(f"{os.getppid()} {old}")
    with al.app_lock("Mail", wait_s=0.5):
        pass


def test_apps_do_not_block_each_other(lock_dir):
    with al.app_lock("Mail", wait_s=0.1):
        with al.app_lock("Notes", wait_s=0.1):
            pass
