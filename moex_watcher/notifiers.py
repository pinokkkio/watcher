"""Каналы уведомлений: консоль, Telegram, webhook, JSONL-журнал."""
from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

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


def build_proxy_url(proxy: str, user: str | None = None, password: str | None = None) -> str:
    """Собирает URL прокси с логином/паролем (спецсимволы экранируются).

    proxy: "https://host:port" — TLS до самого прокси, "http://host:port" — обычный CONNECT-прокси.
    Без схемы считается https://. Логин/пароль можно передать и прямо в URL."""
    if "://" not in proxy:
        proxy = "https://" + proxy
    parts = urlsplit(proxy)
    if parts.scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"Неподдерживаемая схема прокси: {parts.scheme}")
    if not parts.hostname or not parts.port:
        raise ValueError("Прокси должен быть в виде scheme://host:port")
    user = user if user is not None else (unquote(parts.username) if parts.username else None)
    password = password if password is not None else (unquote(parts.password) if parts.password else None)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    auth = ""
    if user:
        auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    return f"{parts.scheme}://{auth}{host}:{parts.port}"


def mask_proxy(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        return url.replace(":" + parts.password + "@", ":***@", 1)
    return url


def _combined_ca_bundle(extra_ca: str) -> str:
    import certifi
    import tempfile

    with open(certifi.where(), encoding="utf-8") as f:
        base = f.read()
    with open(extra_ca, encoding="utf-8") as f:
        extra = f.read()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".pem", prefix="tg-ca-", delete=False, encoding="utf-8")
    with tmp:
        tmp.write(base + "\n" + extra)
    return tmp.name


class TelegramNotifier:
    LIMIT = 4000

    def __init__(self, token: str, chat_id: str, proxy: str | None = None,
                 proxy_user: str | None = None, proxy_password: str | None = None,
                 api_base: str = "https://api.telegram.org", session: requests.Session | None = None,
                 retries: int = 3, proxy_ca: str | None = None) -> None:
        self.url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.retries = retries
        self.session = session or requests.Session()
        self.proxy_display = None
        if proxy:
            url = build_proxy_url(proxy, proxy_user, proxy_password)
            # прокси применяется только к этой сессии (не к ISS), переменные окружения игнорируются
            self.session.trust_env = False
            self.session.proxies = {"http": url, "https": url}
            self.proxy_display = mask_proxy(url)
            if proxy_ca:
                # самоподписанный сертификат https-прокси: доверяем ему в дополнение к стандартным CA
                self.session.verify = _combined_ca_bundle(proxy_ca)
            log.info("Telegram: запросы через прокси %s", self.proxy_display)

    def _post(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(self.url, timeout=20, json=payload)
            except requests.exceptions.ProxyError as e:
                log.warning("Telegram: ошибка прокси %s (%s), попытка %d/%d", self.proxy_display,
                            type(e).__name__, attempt, self.retries)
            except requests.RequestException as e:
                log.warning("Telegram недоступен: %s, попытка %d/%d", type(e).__name__, attempt, self.retries)
            else:
                if r.status_code == 200:
                    return True
                if r.status_code == 407:
                    log.error("Telegram: прокси отклонил логин/пароль (407)")
                    return False
                if r.status_code == 429:
                    try:
                        wait = float(r.json().get("parameters", {}).get("retry_after", 5))
                    except ValueError:
                        wait = 5.0
                    log.warning("Telegram: лимит запросов, жду %.0f с", wait)
                    time.sleep(min(wait, 60))
                    continue
                if r.status_code < 500:
                    log.error("Telegram: %s %s", r.status_code, r.text[:300])
                    return False
                log.warning("Telegram: %s, попытка %d/%d", r.status_code, attempt, self.retries)
            time.sleep(min(2 ** attempt, 10))
        return False

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

    def check(self, text: str) -> bool:
        return self._post(text)


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
