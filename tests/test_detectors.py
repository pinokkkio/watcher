from datetime import datetime, timedelta

from moex_watcher.config import ALL_SIGNALS, Config, Thresholds
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


def feed(d, b, prices, t, step=timedelta(seconds=30)):
    """Подаёт цены по одной каждые 30 сек, возвращает (все алерты, время после последней)."""
    out = []
    for p in prices:
        out += d.process([q(p)], b, t)
        t += step
    return out, t


def test_defaults_only_current_moves():
    d = Detector(Config())
    b = base(sigma=0.005, avg_value=1e9)
    # +6% за день, 12σ, оборот ×7 — но всё это дневные сигналы, по умолчанию выключены
    assert d.process([q(106, value=1e9)], b, T0) == []


def test_move_within_15_min_window():
    d = Detector(Config())
    b = {}  # без истории — проверяем только порог в %
    al, t = feed(d, b, [100.0] * 20, T0)
    assert al == []
    al, t = feed(d, b, [100.5, 101.0, 101.5, 102.1], t)  # +2.1% за 2 мин
    assert kinds(al) == ["FAST_MOVE"]
    assert "+2.1% за 2 мин" in al[0].reasons[0].short


def test_slow_drift_outside_window_ignored():
    d = Detector(Config())
    b = {}  # без истории — проверяем только порог в %
    # +3% за 60 минут равномерно: в любом 15-минутном окне меньше 1%
    prices = [100 * (1 + 0.03 * i / 120) for i in range(121)]
    al, _ = feed(d, b, prices, T0)
    assert al == []


def test_reversal_inside_window_is_caught():
    d = Detector(Config())
    b = {}  # без истории — проверяем только порог в %
    # просели на 1.5% (ниже порога) и отросли: от начала окна +0.6%, но от минимума +2.1%
    al, t = feed(d, b, [100, 99, 98.5, 99.5, 100.6], T0)
    assert kinds(al) == ["FAST_MOVE"]
    assert al[0].reasons[0].short.startswith("+2.1%")


def test_opposite_direction_not_muted_by_cooldown():
    d = Detector(Config())
    b = {}
    al, t = feed(d, b, [100, 97.9], T0)  # -2.1%
    assert al and al[0].reasons[0].short.startswith("-2.1%")
    al, t = feed(d, b, [99.0, 100.0], t)  # отскок +2.1% от минимума — сообщаем, хотя пауза
    assert al and al[0].reasons[0].short.startswith("+2.1%")


def test_cooldown_and_escalation():
    d = Detector(Config())
    b = {}  # без истории — проверяем только порог в %
    al, t = feed(d, b, [100, 102.2], T0)
    assert kinds(al) == ["FAST_MOVE"]
    al, t = feed(d, b, [102.5, 103.0], t)  # продолжение в паузе — тихо
    assert al == []
    al, t = feed(d, b, [104.5], t)  # движение удвоилось (+4.5%) — сообщаем сразу
    assert kinds(al) == ["FAST_MOVE"]
    al, t = feed(d, b, [104.5] * 40, t)  # 20 мин стоит на месте
    assert al == []
    al, t = feed(d, b, [102.3], t)  # новое движение вниз после паузы
    assert kinds(al) == ["FAST_MOVE"] and al[0].reasons[0].short.startswith("-2.1%")


def test_anomaly_fast_for_quiet_instrument():
    d = Detector(all_cfg())
    b = base(sigma=0.004)  # очень спокойная бумага
    t = T0
    for _ in range(10):
        d.process([q(100.0)], b, t)
        t += timedelta(seconds=30)
    al = d.process([q(100.6)], b, t)  # +0.6% за 5 мин: ниже 1.5%, но ≫ σ окна
    assert "ANOMALY_FAST" in kinds(al) and "FAST_MOVE" not in kinds(al)


def test_volume_spike():
    d = Detector(all_cfg())
    # 12:00 МСК — прошло ~14% сессии; средний оборот 1 млрд → ожидаем ~140 млн, факт 1 млрд
    al = d.process([q(100.1, value=1e9)], base(sigma=0.05, avg_value=1e9), T0)
    assert kinds(al) == ["VOLUME_SPIKE"]
    assert kinds(d.process([q(100.1, value=1.2e9)], base(sigma=0.05, avg_value=1e9), T0)) == []


def test_illiquid_filtered():
    d = Detector(all_cfg())
    assert d.process([q(120, value=1e5)], base(), T0) == []
    assert d.process([q(120, value=1e8)], base(avg_value=1e6), T0) == []


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
