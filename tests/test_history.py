from datetime import date

from moex_watcher.history import HistoryStore
from moex_watcher.markets import DEFAULT_MARKETS

from helpers import FakeHistory

SPEC = DEFAULT_MARKETS["shares"]


def test_ensure_and_baselines():
    src = FakeHistory({"LOWV": (0.01, 1e9), "HIGHV": (0.05, 5e8)})
    store = HistoryStore()
    today = date(2026, 11, 20)
    store.ensure(SPEC, src, days=40, today=today)
    b = store.baselines(SPEC.board, 40)
    assert set(b) == {"LOWV", "HIGHV"}
    assert b["LOWV"].n == 40
    assert 0.006 < b["LOWV"].sigma_daily < 0.014
    assert 0.03 < b["HIGHV"].sigma_daily < 0.07
    assert 0.8e9 < b["LOWV"].avg_value < 1.2e9

    # повторный вызов идёт только в кэш
    n_calls = len(src.calls)
    assert store.ensure(SPEC, src, days=40, today=today) == 0
    assert len(src.calls) == n_calls


def test_recent_empty_days_not_cached():
    class Empty:
        def history_day(self, spec, day):
            return []

    store = HistoryStore()
    store.ensure(SPEC, Empty(), days=5, today=date(2026, 11, 20))
    cached = {r[0] for r in store.db.execute("SELECT d FROM fetched")}
    assert "2026-11-19" not in cached  # итоги могли ещё не выйти
    assert "2026-11-10" in cached
