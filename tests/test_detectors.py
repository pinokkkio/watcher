from datetime import datetime, timedelta

from moex_watcher.config import Config
from moex_watcher.detectors import MSK, Detector
from moex_watcher.models import Baseline, Quote

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=MSK)


def q(last, prev=100.0, secid="SBER", value=5e8, cls="shares", board="TQBR"):
    return Quote(market="shares", asset_class=cls, board=board, secid=secid, name=secid, last=last, prev=prev,
                 value_today=value)


def base(sigma=0.02, avg_value=2e9, secid="SBER", board="TQBR"):
    return {board: {secid: Baseline(secid=secid, n=60, sigma_daily=sigma, avg_value=avg_value, atr_pct=2.0)}}


def kinds(alerts):
    return sorted(k for a in alerts for k in a.kinds)


def test_strong_day_levels_escalate_once():
    d = Detector(Config())
    b = base(sigma=0.05)  # высокая σ — без аномалий
    assert kinds(d.process([q(102)], b, T0)) == []
    assert kinds(d.process([q(103.2)], b, T0 + timedelta(minutes=30))) == ["STRONG_DAY"]
    assert kinds(d.process([q(104)], b, T0 + timedelta(minutes=60))) == []  # тот же уровень
    assert kinds(d.process([q(105.5)], b, T0 + timedelta(minutes=90))) == ["STRONG_DAY"]  # 5%
    # разворот вниз — отдельное направление
    al = d.process([q(96.5)], b, T0 + timedelta(minutes=120))
    assert "STRONG_DAY" in kinds(al)


def test_anomaly_day_relative_to_own_volatility():
    d = Detector(Config())
    # 2.5% — не «сильное» по порогу 3%, но для бумаги с σ=0.5% это 5σ
    al = d.process([q(102.5)], base(sigma=0.005), T0)
    assert kinds(al) == ["ANOMALY_DAY"]
    assert al[0].is_anomaly
    # повтор только при росте на z_step
    assert kinds(d.process([q(102.6)], base(sigma=0.005), T0 + timedelta(minutes=20))) == []
    assert kinds(d.process([q(103.1)], base(sigma=0.005), T0 + timedelta(minutes=40))) == \
        ["ANOMALY_DAY", "STRONG_DAY"]


def test_fast_move_and_cooldown():
    d = Detector(Config())
    b = base(sigma=0.05)
    t = T0
    for i in range(10):  # спокойно, каждые 30 сек
        assert d.process([q(100.0)], b, t) == []
        t += timedelta(seconds=30)
    al = d.process([q(101.8)], b, t)
    assert "FAST_MOVE" in kinds(al)
    # в пределах cooldown не повторяем
    t += timedelta(seconds=30)
    assert "FAST_MOVE" not in kinds(d.process([q(99.5)], b, t))


def test_anomaly_fast_for_quiet_instrument():
    d = Detector(Config())
    b = base(sigma=0.004)  # очень спокойная бумага
    t = T0
    for _ in range(10):
        d.process([q(100.0)], b, t)
        t += timedelta(seconds=30)
    al = d.process([q(100.6)], b, t)  # +0.6% за 5 мин: ниже 1.5%, но ≫ σ окна
    assert "ANOMALY_FAST" in kinds(al) and "FAST_MOVE" not in kinds(al)


def test_volume_spike():
    d = Detector(Config())
    # 12:00 МСК — прошло ~14% сессии; средний оборот 1 млрд → ожидаем ~140 млн, факт 1 млрд
    al = d.process([q(100.1, value=1e9)], base(sigma=0.05, avg_value=1e9), T0)
    assert kinds(al) == ["VOLUME_SPIKE"]
    assert kinds(d.process([q(100.1, value=1.2e9)], base(sigma=0.05, avg_value=1e9), T0)) == []


def test_illiquid_filtered():
    d = Detector(Config())
    assert d.process([q(120, value=1e5)], base(), T0) == []
    assert d.process([q(120, value=1e8)], base(avg_value=1e6), T0) == []


def test_state_resets_next_day():
    d = Detector(Config())
    b = base(sigma=0.05)
    assert kinds(d.process([q(104)], b, T0)) == ["STRONG_DAY"]
    assert kinds(d.process([q(104)], b, T0 + timedelta(days=1))) == ["STRONG_DAY"]


def test_bond_thresholds_tighter():
    d = Detector(Config())
    b = base(sigma=0.02, secid="SU26238RMFS4", board="TQOB")
    al = d.process([q(101.2, secid="SU26238RMFS4", cls="bonds", board="TQOB")], b, T0)
    assert kinds(al) == ["STRONG_DAY"]
