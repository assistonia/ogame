import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import run_universe


def test_registered_ai_accounts_use_policy_engine():
    assert run_universe.is_ai_account("ai01@bots.local")
    assert run_universe.is_ai_account("pirate01@bots.local")
    assert run_universe.is_ai_account("explorer01@bots.local")
    assert not run_universe.is_ai_account("pirate01@example.com")
    assert not run_universe.is_ai_account("pirate02@bots.local")
    assert not run_universe.is_ai_account("bot050@bots.local")


def test_singleton_lock_rejects_second_scheduler(tmp_path):
    path = tmp_path / "universe.lock"
    first = run_universe.SingletonLock(path)
    second = run_universe.SingletonLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
