from datetime import datetime, timedelta

from moex_watcher.config import ALL_SIGNALS, Config, load_config
from moex_watcher.detectors import MSK
from moex_watcher.engine import Watcher
from moex_watcher.history import HistoryStore
from moex_watcher.markets import DEFAULT_MARKETS
from moex_watcher.models import Quote
from moex_watcher.notifiers import format_alert
from moex_watcher.volatility import format_table, rank_historical, rank_intraday

from helpers import FakeHistory


class FakeSource(FakeHistory):
    def __init__(self):
        super().__init__({"CALM": (0.008, 2e9), "WILD": (0.04, 1e9), "THIN": (0.03, 1e5)})
        self.prices = {"CALM": 100.0, "WILD": 100.0, "THIN": 100.0}

    def snapshot(self, spec):
        return [Quote(spec.key, spec.asset_class, spec.board, s, s, last=p, prev=100.0, high=max(p, 100),
                      low=min(p, 100), value_today=5e8) for s, p in self.prices.items()]


class Collect:
    def __init__(self):
        self.alerts, self.texts = [], []

    def send_alerts(self, a):
        self.alerts.extend(a)

    def send_text(self, t):
        self.texts.append(t)


def test_watcher_end_to_end():
    cfg = Config(markets=[DEFAULT_MARKETS["shares"]], history_days=30, signals=frozenset(ALL_SIGNALS))
    src, out = FakeSource(), Collect()
    w = Watcher(cfg, src, HistoryStore(), out)
    w.refresh_baselines(force=True)
    b = w.baselines["TQBR"]
    assert b["WILD"].sigma_daily > b["CALM"].sigma_daily

    t = datetime(2026, 11, 20, 15, 0, tzinfo=MSK)
    src.prices.update(CALM=103.0, WILD=103.0)  # +3%: для CALM это ~3.7σ, для WILD ~0.75σ
    w.tick(now=t)
    by = {a.quote.secid: a for a in out.alerts}
    assert by["CALM"].is_anomaly and "ANOMALY_DAY" in by["CALM"].kinds
    assert not by["WILD"].is_anomaly and by["WILD"].kinds == ["STRONG_DAY"]
    assert "THIN" not in by  # исторически неликвидна
    text = format_alert(by["CALM"])
    assert "АНОМАЛИЯ" in text and "CALM" in text

    hist = rank_historical(b, "shares", top=2)
    assert hist[0].secid == "WILD"
    intr = rank_intraday(w.quotes["shares"], w.baselines, by="z")
    assert intr[0].secid == "CALM"
    assert "WILD" in format_table(hist) and "Самые волатильные" in w.digest()


def test_load_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general]\nwatch=["shares","futures"]\npoll_interval_sec=10\n'
                 '[thresholds.shares]\nfast_move_pct=2.5\n[telegram]\ntoken="x"\nchannel="@moex_alerts"\n', encoding="utf-8")
    cfg = load_config(p)
    assert [m.board for m in cfg.markets] == ["TQBR", "RFUD"]
    assert cfg.th("shares").fast_move_pct == 2.5
    assert cfg.th("shares").day_levels == [3.0, 5.0, 7.0, 10.0, 15.0]
    assert cfg.th("bonds").fast_move_pct == 1.0
    assert cfg.telegram_channel == "@moex_alerts"


def test_signals_config(tmp_path, monkeypatch):
    import pytest
    from moex_watcher.cli import startup_message

    cfg = load_config(None)
    assert cfg.signals == {"FAST_MOVE", "ANOMALY_FAST"} and cfg.digest_interval_min == 0
    msg = startup_message(cfg)
    assert "движение за последние 15 мин: акции ≥2%, облигации ≥1%, фьючерсы ≥1.5%" in msg
    assert "за день" not in msg
    monkeypatch.setenv("WATCH_SIGNALS", "fast_move,strong_day")
    assert load_config(None).signals == {"FAST_MOVE", "STRONG_DAY"}
    monkeypatch.setenv("WATCH_SIGNALS", "nope")
    with pytest.raises(ValueError):
        load_config(None)
