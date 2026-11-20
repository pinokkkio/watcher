from datetime import datetime, timedelta

from moex_watcher.config import Config
from moex_watcher.detectors import MSK
from moex_watcher.engine import Watcher
from moex_watcher.history import HistoryStore
from moex_watcher.iss import parse_candles
from moex_watcher.markets import DEFAULT_MARKETS
from moex_watcher.models import Candle, Quote
from moex_watcher.scanner import MoveScanner, strongest_move

SHARES = DEFAULT_MARKETS["shares"]
D0 = datetime(2026, 11, 20, 13, 0)  # время данных ISS (МСК, naive)


def mins(prices, start=D0):
    """Минутные свечи из списка цен закрытия (или кортежей (low, high, close))."""
    out = []
    for i, p in enumerate(prices):
        lo, hi, c = p if isinstance(p, tuple) else (p, p, p)
        b = start + timedelta(minutes=i)
        out.append(Candle(b, b + timedelta(minutes=1) - timedelta(seconds=1), hi, lo, c))
    return out


def test_parse_candles():
    payload = {"candles": {"columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
                           "data": [[100, 101, 102, 99.5, 1e6, 10, "2026-11-20 13:00:00", "2026-11-20 13:00:59"],
                                    [None, None, None, None, 0, 0, "2026-11-20 13:01:00", "2026-11-20 13:01:59"]]}}
    (c,) = parse_candles(payload)
    assert (c.high, c.low, c.close, c.begin) == (102, 99.5, 101, datetime(2026, 11, 20, 13, 0))


def test_strongest_move_inside_window():
    w = timedelta(minutes=15)
    # медленный рост +3% за 60 минут — ни в одном 15-минутном окне нет 2%
    slow = mins([100 * (1 + 0.03 * i / 60) for i in range(61)])
    assert abs(strongest_move(slow, w, D0).ret) < 0.01
    # провал на 2.5% и полный отскок внутри окна: от начала до конца 0%, но движение есть
    v = mins([100] * 5 + [99, 98, 97.5, 98.5, 99.5, 100.1] + [100.1] * 10)
    mv = strongest_move(v, w, D0)
    assert round(mv.ret * 100, 1) == 2.7 and mv.start == D0 + timedelta(minutes=7)
    assert mv.end == D0 + timedelta(minutes=10)


def test_since_limits_to_new_period():
    w = timedelta(minutes=15)
    c = mins([100, 103] + [103] * 40)
    assert strongest_move(c, w, D0).ret > 0.02
    # рывок был в 13:01, а новый период начинается с 13:25 — в его окна рывок не попадает
    assert strongest_move(c, w, D0 + timedelta(minutes=25)) is None


class Src:
    def __init__(self, series):
        self.series = series  # secid -> list[Candle]
        self.calls = []

    def candles(self, spec, secid, start):
        self.calls.append((secid, start))
        return [c for c in self.series.get(secid, []) if c.begin >= start]


def q(secid, low, high, last=None, cls="shares", board="TQBR", market="shares"):
    return Quote(market, cls, board, secid, secid, last=last or high, prev=100, high=high, low=low)


def test_only_candidates_are_requested():
    src = Src({"MOVE": mins([100] * 10 + [101, 102, 102.5] + [102.5] * 3)})
    sc = MoveScanner(Config(), src)
    now = datetime(2026, 11, 20, 13, 31, tzinfo=MSK)  # данные идут с задержкой ~15 мин
    quotes = [q("MOVE", 99.8, 102.6), q("CALM", 100, 101.5)]  # у CALM дневной диапазон 1.5% < 2%
    al = sc.scan(SHARES, quotes, now)
    assert [c[0] for c in src.calls] == ["MOVE"]
    (a,) = al
    assert a.reasons[0].short == "+2.5% за 3 мин (13:09–13:12)"
    assert a.quote.last == 102.5


def test_startup_then_every_15_minutes_no_duplicates():
    # рывок +3% в 13:05–13:07, затем ровно, затем ещё +3.1% в 13:25–13:27
    full = mins([100] * 5 + [101, 102, 103] + [103] * 17 + [104, 105.5, 106.2] + [106.2] * 15)
    src = Src({})
    sc = MoveScanner(Config(), src)
    quotes = [q("X", 100, 106.2)]

    def check(data_until_min, wall):
        src.series["X"] = [c for c in full if c.begin < D0 + timedelta(minutes=data_until_min)]
        return sc.scan(SHARES, quotes, datetime(2026, 11, 20, *wall, tzinfo=MSK))

    # старт: данные по 13:19 включительно — видим рывок 13:05
    (a,) = check(20, (13, 35))
    assert a.reasons[0].short == "+3.0% за 3 мин (13:04–13:07)"
    # через 15 минут: старый рывок не повторяется, новый найден
    (a,) = check(35, (13, 50))
    assert a.reasons[0].short == "+3.1% за 3 мин (13:24–13:27)"
    # ещё через 15 минут: окна всё ещё захватывают начало рывка 13:24, но он уже сообщён
    assert check(50, (14, 5)) == []
    # запрос свечей — с запасом назад: прошедший период + окно + задержка данных
    assert src.calls[-1][1] == datetime(2026, 11, 20, 13, 15)


def test_bond_and_futures_thresholds():
    bonds, fut = DEFAULT_MARKETS["corp_bonds"], DEFAULT_MARKETS["futures"]
    src = Src({"B1": mins([100] * 5 + [101.9]), "B2": mins([100] * 5 + [102.1]),
               "F1": mins([100] * 5 + [102.9]), "F2": mins([100] * 5 + [103.1])})
    now = datetime(2026, 11, 20, 13, 21, tzinfo=MSK)
    bq = [q(s, 100, 103, cls="bonds", board="TQCB", market="corp_bonds") for s in ("B1", "B2")]
    fq = [q(s, 100, 104, cls="futures", board="RFUD", market="futures") for s in ("F1", "F2")]
    assert [a.quote.secid for a in MoveScanner(Config(), src).scan(bonds, bq, now)] == ["B2"]
    assert [a.quote.secid for a in MoveScanner(Config(), src).scan(fut, fq, now)] == ["F2"]


def test_watcher_one_message_per_check():
    class Source(Src):
        def snapshot(self, spec):
            return [q("A", 100, 103), q("B", 100, 104)]

        def history_day(self, spec, day):
            raise AssertionError("дневная история не нужна без дневных сигналов")

    class Collect:
        def __init__(self):
            self.messages = []
            self.pending = []

        def send_alerts(self, a):
            self.pending += a

        def send_text(self, t):
            pass

        def send_status(self, t):
            pass

        def flush(self, force=False):
            if self.pending:
                self.messages.append(self.pending)
            self.pending = []

    now = datetime.now(MSK).replace(tzinfo=None)
    start = now - timedelta(minutes=10)
    src = Source({"A": mins([100] * 3 + [103], start), "B": mins([100] * 3 + [104], start)})
    out = Collect()
    Watcher(Config(markets=[SHARES]), src, HistoryStore(), out).run(once=True)
    assert len(out.messages) == 1 and sorted(a.quote.secid for a in out.messages[0]) == ["A", "B"]


def test_trend_continuation_is_reported_again():
    # +3% к 13:03, затем тренд продолжается: ещё +3% к 13:20 — это новый отрезок
    full = mins([100, 101.5, 103] + [103] * 12 + [104, 105, 106.2] + [106.2] * 10)
    src = Src({"X": full[:10]})
    sc = MoveScanner(Config(), src)
    quotes = [q("X", 100, 106.2)]
    (a,) = sc.scan(SHARES, quotes, datetime(2026, 11, 20, 13, 25, tzinfo=MSK))
    assert a.reasons[0].short == "+3.0% за 2 мин (13:00–13:02)"
    src.series["X"] = full
    (a,) = sc.scan(SHARES, quotes, datetime(2026, 11, 20, 13, 40, tzinfo=MSK))
    assert a.reasons[0].short == "+3.1% за 3 мин (13:14–13:17)"  # новый отрезок, не повтор первого


def test_reversal_reported_after_rise():
    full = mins([100, 101.5, 103] + [103] * 5 + [102, 100.5, 99.8] + [99.8] * 5)
    src = Src({"X": full[:6]})
    sc = MoveScanner(Config(), src)
    quotes = [q("X", 99.8, 103)]
    (a,) = sc.scan(SHARES, quotes, datetime(2026, 11, 20, 13, 21, tzinfo=MSK))
    assert a.reasons[0].move_pct > 0
    src.series["X"] = full
    (a,) = sc.scan(SHARES, quotes, datetime(2026, 11, 20, 13, 36, tzinfo=MSK))
    assert a.reasons[0].short.startswith("-3.1%")
