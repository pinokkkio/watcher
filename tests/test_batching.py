from datetime import datetime

from moex_watcher.models import Alert, Quote, Reason
from moex_watcher.notifiers import TelegramNotifier, format_batch


def make_tg(max_lines=30):
    tg = TelegramNotifier("t", "@moex_alerts", max_lines=max_lines)
    sent = []
    tg._post = lambda text: sent.append(text) or True
    return tg, sent


def alert(secid, move, kind="FAST_MOVE", name=None, market="shares"):
    q = Quote(market, "shares", "TQBR", secid, name or secid, last=100 + move, prev=100.0)
    text = f"{move:+.1f}% за 3 мин (13:09–13:12)"
    return Alert(q, [Reason(kind, text, False, text, move)], ts=datetime.now())


def test_one_message_per_check():
    tg, sent = make_tg()
    tg.send_alerts([alert("SBER", 2.5), alert("GAZP", -4.0)])
    tg.send_alerts([alert("LKOH", 3.2)])
    assert sent == []  # до конца проверки ничего не отправляется
    tg.flush()
    assert len(sent) == 1
    msg = sent[0]
    assert "сигналов: 3" in msg
    assert msg.index("GAZP") < msg.index("LKOH") < msg.index("SBER")  # по силе движения
    assert "🔴 <b>GAZP</b>" in msg and "🟢 <b>SBER</b>" in msg
    tg.flush()
    assert len(sent) == 1  # пустой буфер — без сообщения


def test_same_instrument_is_merged():
    tg, sent = make_tg()
    tg.send_alerts([alert("SBER", 2.1)])
    tg.send_alerts([alert("SBER", 2.6)])
    tg.flush()
    (msg,) = sent
    assert msg.count("<b>SBER</b>") == 1 and "+2.6%" in msg and "+2.1%" not in msg


def test_many_alerts_capped_into_single_message():
    tg, sent = make_tg(max_lines=10)
    tg.send_alerts([alert(f"T{i:03d}", 2 + i / 10, name="Очень длинное название эмитента " * 3)
                    for i in range(300)])
    tg.flush()
    (msg,) = sent
    assert len(msg) <= 4000
    assert msg.count("🟢") == 10
    assert "…и ещё 290" in msg
    assert "T299" in msg  # самые сильные — вверху


def test_cap_respects_telegram_limit():
    alerts = [alert(f"T{i:03d}", 2 + i / 100, name="Н" * 60) for i in range(100)]
    msg = format_batch(alerts, max_lines=100)
    assert len(msg) <= 4000 and "…и ещё" in msg


def test_digest_joins_pending_alerts():
    tg, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.5)])
    tg.send_text("Самые волатильные сегодня — Акции:\n1. VKCO <x>")
    tg.flush()
    (msg,) = sent
    assert "SBER" in msg and "Самые волатильные" in msg and "&lt;x&gt;" in msg


def test_status_is_immediate():
    tg, sent = make_tg()
    tg.send_alerts([alert("SBER", 3.5)])
    tg.send_status("запущен <ok>")
    assert sent == ["запущен &lt;ok&gt;"]
