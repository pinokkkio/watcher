"""Каналы уведомлений: консоль, Telegram, webhook, JSONL-журнал."""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Protocol

import requests

from .models import Alert

log = logging.getLogger(__name__)

CLASS_LABEL = {"shares": "Акция", "bonds": "Облигация", "futures": "Фьючерс"}


def format_alert(a: Alert, markup: bool = False) -> str:
    q = a.quote
    chg = q.change_pct
    up = (chg or 0) >= 0
    icon = ("⚠️" if a.is_anomaly else "") + ("🟢" if up else "🔴")
    tag = "АНОМАЛИЯ" if a.is_anomaly else "Сильное движение"
    unit = "%" if q.asset_class == "bonds" else ""
    secid = f"<b>{html.escape(q.secid)}</b>" if markup else q.secid
    name = html.escape(q.name) if markup else q.name
    head = f"{icon} {tag}: {secid} ({name}, {CLASS_LABEL.get(q.asset_class, q.asset_class)} {q.board})"
    price = f"Цена {q.last:g}{unit}" + (f", {chg:+.2f}% за день" if chg is not None else "")
    body = [f"• {html.escape(r.text) if markup else r.text}" for r in a.reasons]
    return "\n".join([head, price, *body])


class Notifier(Protocol):
    def send_alerts(self, alerts: list[Alert]) -> None: ...
    def send_text(self, text: str) -> None: ...


class ConsoleNotifier:
    def send_alerts(self, alerts: list[Alert]) -> None:
        for a in alerts:
            ts = a.ts.strftime("%H:%M:%S") if a.ts else ""
            print(f"[{ts}] {format_alert(a)}\n", flush=True)

    def send_text(self, text: str) -> None:
        print(text + "\n", flush=True)


class JsonlNotifier:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send_alerts(self, alerts: list[Alert]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            for a in alerts:
                q = a.quote
                f.write(json.dumps({
                    "ts": a.ts.isoformat() if a.ts else None, "market": q.market, "board": q.board,
                    "secid": q.secid, "name": q.name, "last": q.last, "prev": q.prev,
                    "change_pct": q.change_pct, "value_today": q.value_today, "anomaly": a.is_anomaly,
                    "reasons": [{"kind": r.kind, "text": r.text} for r in a.reasons],
                }, ensure_ascii=False) + "\n")

    def send_text(self, text: str) -> None:
        pass


class TelegramNotifier:
    LIMIT = 4000

    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def _post(self, text: str) -> None:
        try:
            r = self.session.post(self.url, timeout=15, json={
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            if r.status_code != 200:
                log.warning("Telegram: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram недоступен: %s", e)

    def _send_chunks(self, parts: list[str]) -> None:
        buf = ""
        for p in parts:
            if buf and len(buf) + len(p) + 2 > self.LIMIT:
                self._post(buf)
                buf = ""
            buf = f"{buf}\n\n{p}" if buf else p
        if buf:
            self._post(buf[: self.LIMIT])

    def send_alerts(self, alerts: list[Alert]) -> None:
        # аномалии — первыми
        ordered = sorted(alerts, key=lambda a: not a.is_anomaly)
        self._send_chunks([format_alert(a, markup=True) for a in ordered])

    def send_text(self, text: str) -> None:
        self._send_chunks([f"<pre>{html.escape(text)}</pre>"])


class WebhookNotifier:
    def __init__(self, url: str) -> None:
        self.url = url

    def _post(self, payload: dict) -> None:
        try:
            requests.post(self.url, json=payload, timeout=10)
        except requests.RequestException as e:
            log.warning("Webhook недоступен: %s", e)

    def send_alerts(self, alerts: list[Alert]) -> None:
        self._post({"type": "alerts", "items": [
            {"secid": a.quote.secid, "board": a.quote.board, "anomaly": a.is_anomaly,
             "change_pct": a.quote.change_pct, "last": a.quote.last, "text": format_alert(a),
             "kinds": a.kinds} for a in alerts]})

    def send_text(self, text: str) -> None:
        self._post({"type": "digest", "text": text})


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send_alerts(self, alerts: list[Alert]) -> None:
        if alerts:
            for n in self.notifiers:
                n.send_alerts(alerts)

    def send_text(self, text: str) -> None:
        for n in self.notifiers:
            n.send_text(text)
