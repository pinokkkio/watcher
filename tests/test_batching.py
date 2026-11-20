from datetime import datetime

from moex_watcher.config import Config
from moex_watcher.engine import Watcher
from moex_watcher.models import Alert, Quote, Reason
from moex_watcher.notifiers import MultiNotifier, TelegramNotifier, format_batch


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_tg(batch_sec=60.0, max_lines=30):
    clock = Clock()
    tg = TelegramNotifier("t", "@moex_alerts", batch_sec=batch_sec, max_lines=max_lines, clock=clock)
    sent = []
    tg._post = lambda text: sent.append(text) or True
    return tg, clock, sent


def alert(secid, chg, kind="STRONG_DAY", anomaly=False, market="shares", name=None):
    q = Quote(market, "shares", "TQBR", secid, name or secid, last=100 + chg, prev=100.0)
    return Alert(q, [Reason(kind, f"{kind} long", anomaly, f"{kind} short")], ts=datetime.now())


def test_alerts_within_window_go_as_one_message():
    tg, clock, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.5), alert("GAZP", -4.0)])
    clock.t += 30
    tg.send_alerts([alert("LKOH", 5.2, "ANOMALY_DAY", True)])
    tg.flush()
    assert sent == []  # окно ещё не истекло
    clock.t += 31
    tg.flush()
    assert len(sent) == 1
    msg = sent[0]
    assert "сигналов: 3" in msg
    # аномалии первыми, затем сильные движения по убыванию |изменения|
    assert msg.index("Аномалии") < msg.index("LKOH") < msg.index("Сильные движения") < msg.index("GAZP") \
        < msg.index("SBER")
    tg.flush(force=True)
    assert len(sent) == 1  # буфер пуст


def test_same_instrument_is_merged():
    tg, clock, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.1)])
    tg.send_alerts([alert("SBER", 5.4, "ANOMALY_DAY", True)])
    tg.send_alerts([alert("SBER", 5.6, "FAST_MOVE")])
    tg.flush(force=True)
    (msg,) = sent
    assert msg.count("<b>SBER</b>") == 1
    assert "сигналов: 1" in msg and "+5.60%" in msg  # свежая цена
    line = next(l for l in msg.splitlines() if "SBER" in l)
    assert line.index("STRONG_DAY") < line.index("ANOMALY_DAY") < line.index("FAST_MOVE")


def test_many_alerts_capped_into_single_message():
    tg, clock, sent = make_tg(max_lines=10)
    tg.send_alerts([alert(f"T{i:03d}", 3 + i / 10, name="Очень длинное название эмитента " * 3) for i in range(300)])
    tg.flush(force=True)
    (msg,) = sent
    assert len(msg) <= 4000
    assert msg.count("🟢") == 10
    assert "…и ещё 290" in msg
    assert "T299" in msg  # самые сильные — вверху


def test_cap_respects_telegram_limit():
    alerts = [alert(f"T{i:03d}", 3 + i / 100, name="Н" * 60) for i in range(100)]
    msg = format_batch(alerts, max_lines=100)
    assert len(msg) <= 4000 and "…и ещё" in msg


def test_digest_joins_pending_alerts():
    tg, clock, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.5)])
    tg.send_text("Самые волатильные сегодня — Акции:\n1. VKCO <x>")
    clock.t += 60
    tg.flush()
    (msg,) = sent
    assert "SBER" in msg and "Самые волатильные" in msg and "&lt;x&gt;" in msg


def test_zero_window_sends_each_tick_once():
    tg, clock, sent = make_tg(batch_sec=0)
    tg.send_alerts([alert("SBER", 3.5), alert("GAZP", 3.6)])
    assert len(sent) == 1
    tg.send_alerts([])
    assert len(sent) == 1


def test_status_is_immediate():
    tg, clock, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.5)])
    tg.send_status("запущен <ok>")
    assert sent == ["запущен &lt;ok&gt;"]


def test_watcher_flushes_on_stop():
    tg, clock, sent = make_tg(batch_sec=600)

    class Src:
        def snapshot(self, spec):
            return [Quote(spec.key, spec.asset_class, spec.board, "SBER", "Сбербанк", last=104.0, prev=100.0,
                          value_today=1e9)]

        def history_day(self, spec, day):
            return []

    from moex_watcher.history import HistoryStore
    from moex_watcher.markets import DEFAULT_MARKETS

    cfg = Config(markets=[DEFAULT_MARKETS["shares"]], history_days=5, digest_interval_min=0)
    w = Watcher(cfg, Src(), HistoryStore(), MultiNotifier([tg]))
    w.run(once=True)
    assert len(sent) == 1 and "SBER" in sent[0]
