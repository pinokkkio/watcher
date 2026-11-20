from datetime import datetime, timedelta

from moex_watcher.config import ALL_SIGNALS, Config
from moex_watcher.detectors import MSK, Detector
from moex_watcher.models import Baseline, Quote

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=MSK)


def q(last, prev=100.0, secid="SBER", value=5e8, cls="shares", board="TQBR"):
    return Quote(market="shares", asset_class=cls, board=board, secid=secid, name=secid, last=last, prev=prev,
                 value_today=value)


def base(sigma=0.02, avg_value=2e9, secid="SBER", board="TQBR"):
    return {board: {secid: Baseline(secid=secid, n=60, sigma_daily=sigma, avg_value=avg_value, atr_pct=2.0)}}


def all_cfg():
    """Все сигналы, включая дневные (по умолчанию выключены)."""
    return Config(signals=frozenset(ALL_SIGNALS))


def kinds(alerts):
    return sorted(k for a in alerts for k in a.kinds)


def test_strong_day_levels_escalate_once():
    d = Detector(all_cfg())
    b = base(sigma=0.05)  # высокая σ — без аномалий
    assert kinds(d.process([q(102)], b, T0)) == []
    assert kinds(d.process([q(103.2)], b, T0 + timedelta(minutes=30))) == ["STRONG_DAY"]
    assert kinds(d.process([q(104)], b, T0 + timedelta(minutes=60))) == []  # тот же уровень
    assert kinds(d.process([q(105.5)], b, T0 + timedelta(minutes=90))) == ["STRONG_DAY"]  # 5%
    # разворот вниз — отдельное направление
    al = d.process([q(96.5)], b, T0 + timedelta(minutes=120))
    assert "STRONG_DAY" in kinds(al)


def test_anomaly_day_relative_to_own_volatility():
    d = Detector(all_cfg())
    # 2.5% — не «сильное» по порогу 3%, но для бумаги с σ=0.5% это 5σ
    al = d.process([q(102.5)], base(sigma=0.005), T0)
    assert kinds(al) == ["ANOMALY_DAY"]
    assert al[0].is_anomaly
    # повтор только при росте на z_step
    assert kinds(d.process([q(102.6)], base(sigma=0.005), T0 + timedelta(minutes=20))) == []
    assert kinds(d.process([q(103.1)], base(sigma=0.005), T0 + timedelta(minutes=40))) == \
        ["ANOMALY_DAY", "STRONG_DAY"]


def test_day_signals_off_by_default():
    d = Detector(Config())
    b = base(sigma=0.005, avg_value=1e9)
    # +6% за день, 12σ — но это дневные сигналы, по умолчанию выключены
    assert d.process([q(106, value=1e9)], b, T0) == []


def test_state_resets_next_day():
    d = Detector(all_cfg())
    b = base(sigma=0.05)
    assert kinds(d.process([q(104)], b, T0)) == ["STRONG_DAY"]
    assert kinds(d.process([q(104)], b, T0 + timedelta(days=1))) == ["STRONG_DAY"]


def test_bond_thresholds_tighter():
    d = Detector(all_cfg())
    b = base(sigma=0.02, secid="SU26238RMFS4", board="TQOB")
    al = d.process([q(101.2, secid="SU26238RMFS4", cls="bonds", board="TQOB")], b, T0)
    assert kinds(al) == ["STRONG_DAY"]
