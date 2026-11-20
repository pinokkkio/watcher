"""Каналы уведомлений: консоль, Telegram, webhook, JSONL-журнал."""
from __future__ import annotations

import html
import json
import re
import logging
import time
from datetime import datetime
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


MARKET_TAG = {"ofz": "ОФЗ", "corp_bonds": "обл.", "futures": "фьюч.", "etf": "БПИФ"}
# порядок причин в строке: день → короткое окно → объём
KIND_ORDER = ["STRONG_DAY", "ANOMALY_DAY", "FAST_MOVE", "ANOMALY_FAST", "VOLUME_SPIKE"]


def merge_alerts(old: Alert, new: Alert) -> Alert:
    """Сливает два алерта по одному инструменту: свежая котировка, последний текст по каждому типу."""
    by_kind = {r.kind: r for r in old.reasons}
    by_kind.update({r.kind: r for r in new.reasons})
    return Alert(quote=new.quote, reasons=list(by_kind.values()), ts=new.ts)


def _kind_rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def alert_line(a: Alert) -> str:
    """Одна строка на инструмент (HTML)."""
    q = a.quote
    chg = q.change_pct
    icon = "🟢" if (chg or 0) >= 0 else "🔴"
    tag = MARKET_TAG.get(q.market)
    name = html.escape(q.name if q.name != q.secid else "")
    unit = "%" if q.asset_class == "bonds" else ""
    head = f"{icon} <b>{html.escape(q.secid)}</b>" + (f" {name}" if name else "") + (f" [{tag}]" if tag else "")
    price = f"{q.last:g}{unit}" + (f" ({chg:+.2f}%)" if chg is not None else "")
    details = "; ".join(html.escape(r.short or r.text) for r in sorted(a.reasons, key=lambda r: _kind_rank(r.kind)))
    return f"{head} {price} — {details}"


def format_batch(alerts: list[Alert], digest: str | None = None, now: datetime | None = None,
                 max_lines: int = 30, limit: int = 4000) -> str:
    """Одно сообщение: аномалии, затем сильные движения (по убыванию |изменения|), затем сводка."""
    def strength(a: Alert) -> float:
        return abs(a.quote.change_pct or 0)

    anomalies = sorted((a for a in alerts if a.is_anomaly), key=strength, reverse=True)
    strong = sorted((a for a in alerts if not a.is_anomaly), key=strength, reverse=True)
    ts = (now or datetime.now().astimezone()).strftime("%H:%M")
    header = f"📊 <b>MOEX · {ts}</b>" + (f" · сигналов: {len(alerts)}" if alerts else "")

    sections: list[tuple[str, list[str]]] = []
    if anomalies:
        sections.append(("⚠️ <b>Аномалии</b>", [alert_line(a) for a in anomalies]))
    if strong:
        sections.append(("📈 <b>Сильные движения</b>", [alert_line(a) for a in strong]))

    def render(budget: int) -> tuple[str, int]:
        parts, shown = [header], 0
        for title, lines in sections:
            take = lines[: max(0, budget - shown)]
            if take:
                parts.append(title + "\n" + "\n".join(take))
                shown += len(take)
        hidden = len(alerts) - shown
        if hidden > 0:
            parts.append(f"…и ещё {hidden} (полный список — в журнале alerts.jsonl)")
        if digest:
            parts.append(html.escape(digest))
        return "\n\n".join(parts), shown

    budget = max_lines
    text, _ = render(budget)
    while len(text) > limit and budget > 0:
        budget -= 1
        text, _ = render(budget)
    return text[:limit]


class Notifier(Protocol):
    def send_alerts(self, alerts: list[Alert]) -> None: ...
    def send_text(self, text: str) -> None: ...
    def send_status(self, text: str) -> None: ...
    def flush(self, force: bool = False) -> None: ...


class ConsoleNotifier:
    def send_alerts(self, alerts: list[Alert]) -> None:
        for a in alerts:
            ts = a.ts.strftime("%H:%M:%S") if a.ts else ""
            print(f"[{ts}] {format_alert(a)}\n", flush=True)

    def send_text(self, text: str) -> None:
        print(text + "\n", flush=True)

    def send_status(self, text: str) -> None:
        print(text + "\n", flush=True)

    def flush(self, force: bool = False) -> None:
        pass


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

    def send_status(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().astimezone().isoformat(), "status": text},
                               ensure_ascii=False) + "\n")

    def flush(self, force: bool = False) -> None:
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


def normalize_channel(value: str) -> str:
    """Приводит адрес канала к виду, который понимает Bot API: "@имя" или числовой id "-100…".

    Принимает: @имя, имя, https://t.me/имя, t.me/имя, -1001234567890."""
    v = str(value).strip()
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)/?$", v)
    if m:
        v = m.group(1)
    if re.fullmatch(r"-?\d+", v):
        if not v.startswith("-100"):
            log.warning("Telegram: %s не похож на id канала (ожидается -100… или @имя) — "
                        "это личный чат или группа", v)
        return v
    name = v.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", name):
        raise ValueError(f"Некорректный адрес канала: {value!r}. Нужно @имя или -100… "
                         "(для приватного канала — числовой id, ссылка-приглашение не подходит)")
    return "@" + name


CHANNEL_HINTS = (
    ("chat not found", "канал не найден — проверьте @имя/id и что бот добавлен в канал"),
    ("not enough rights", "у бота нет права «Публикация сообщений» в канале"),
    ("bot is not a member", "бот не добавлен в канал — добавьте его администратором"),
    ("need administrator rights", "сделайте бота администратором канала с правом публикации"),
    ("bot was kicked", "бот удалён из канала — добавьте его заново администратором"),
)


class TelegramNotifier:

    def __init__(self, token: str, channel: str, proxy: str | None = None,
                 proxy_user: str | None = None, proxy_password: str | None = None,
                 api_base: str = "https://api.telegram.org", session: requests.Session | None = None,
                 retries: int = 3, proxy_ca: str | None = None, batch_sec: float = 60.0,
                 max_lines: int = 30, clock=time.monotonic) -> None:
        self.api = f"{api_base.rstrip('/')}/bot{token}"
        self.url = f"{self.api}/sendMessage"
        self.chat_id = normalize_channel(channel)
        # Сигналы копятся batch_sec секунд и уходят одним сообщением
        self.batch_sec = batch_sec
        self.max_lines = max_lines
        self._clock = clock
        self._pending: dict[tuple[str, str], Alert] = {}
        self._digest: str | None = None
        self._since: float | None = None
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

    def _call(self, method: str, payload: dict) -> dict | None:
        """Вызов Bot API через прокси. Возвращает result или None (причина пишется в лог)."""
        url = f"{self.api}/{method}"
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(url, timeout=20, json=payload)
            except requests.exceptions.ProxyError as e:
                log.warning("Telegram: ошибка прокси %s (%s), попытка %d/%d", self.proxy_display,
                            type(e).__name__, attempt, self.retries)
            except requests.RequestException as e:
                log.warning("Telegram недоступен: %s, попытка %d/%d", type(e).__name__, attempt, self.retries)
            else:
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    if data.get("ok"):
                        return data.get("result") or {}
                    log.error("Telegram %s: неожиданный ответ %s", method, r.text[:200])
                    return None
                if r.status_code == 407:
                    log.error("Telegram: прокси отклонил логин/пароль (407)")
                    return None
                if r.status_code == 429:
                    try:
                        wait = float(r.json().get("parameters", {}).get("retry_after", 5))
                    except ValueError:
                        wait = 5.0
                    log.warning("Telegram: лимит запросов, жду %.0f с", wait)
                    time.sleep(min(wait, 60))
                    continue
                if r.status_code == 401:
                    log.error("Telegram: неверный TELEGRAM_TOKEN (401 Unauthorized)")
                    return None
                if r.status_code < 500:
                    desc = _describe(r)
                    hint = next((h for k, h in CHANNEL_HINTS if k in desc.lower()), None)
                    log.error("Telegram %s %s (%s): %s%s", method, r.status_code, self.chat_id, desc,
                              f" — {hint}" if hint else "")
                    return None
                log.warning("Telegram: %s, попытка %d/%d", r.status_code, attempt, self.retries)
            time.sleep(min(2 ** attempt, 10))
        log.error("Telegram %s: не удалось после %d попыток", method, self.retries)
        return None

    def _post(self, text: str) -> bool:
        res = self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                         "disable_web_page_preview": True})
        if res is None:
            return False
        chat = res.get("chat") or {}
        where = chat.get("title") or chat.get("username") or chat.get("id") or self.chat_id
        log.info("Telegram: опубликовано в «%s» (%s), %d симв.", where, chat.get("type", "?"), len(text))
        if chat.get("type") and chat["type"] != "channel":
            log.warning("Telegram: сообщение ушло в чат типа «%s», а не в канал — проверьте TELEGRAM_CHANNEL",
                        chat["type"])
        return True

    def verify(self) -> bool:
        """Проверка при старте: токен, доступность канала, права бота на публикацию."""
        me = self._call("getMe", {})
        if me is None:
            return False
        bot = f"@{me.get('username')}"
        chat = self._call("getChat", {"chat_id": self.chat_id})
        if chat is None:
            log.error("Telegram: бот %s не видит канал %s", bot, self.chat_id)
            return False
        title, ctype = chat.get("title") or chat.get("username") or self.chat_id, chat.get("type")
        if ctype != "channel":
            log.warning("Telegram: %s — это «%s», а не канал", self.chat_id, ctype)
        member = self._call("getChatMember", {"chat_id": self.chat_id, "user_id": me.get("id")})
        status = (member or {}).get("status")
        if ctype == "channel" and (status != "administrator" or not member.get("can_post_messages", False)):
            if status != "creator":
                log.error("Telegram: бот %s в канале «%s» имеет статус «%s» без права публикации — "
                          "сделайте его администратором с правом «Публикация сообщений»", bot, title, status)
                return False
        log.info("Telegram: бот %s, канал «%s» (%s, id %s), статус бота: %s, прокси: %s, окно склейки %g с",
                 bot, title, ctype, chat.get("id"), status, self.proxy_display or "нет", self.batch_sec)
        return True

    def send_alerts(self, alerts: list[Alert]) -> None:
        for a in alerts:
            old = self._pending.get(a.quote.key)
            self._pending[a.quote.key] = merge_alerts(old, a) if old else a
        if alerts and self._since is None:
            self._since = self._clock()
        if alerts:
            log.info("Telegram: в очереди %d инструм., отправка не позже чем через %.0f с", len(self._pending),
                     max(0.0, self.batch_sec - (self._clock() - self._since)))
        self.flush()

    def send_text(self, text: str) -> None:
        """Сводка уходит в том же сообщении, что и накопленные сигналы."""
        self._digest = text
        if self._since is None:
            self._since = self._clock()
        self.flush()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def flush(self, force: bool = False) -> None:
        if self._since is None:
            return
        if not force and self._clock() - self._since < self.batch_sec:
            return
        alerts, digest = list(self._pending.values()), self._digest
        self._pending, self._digest, self._since = {}, None, None
        if alerts or digest:
            log.info("Telegram: отправляю пачку — сигналов %d%s", len(alerts), ", со сводкой" if digest else "")
            self._post(format_batch(alerts, digest, max_lines=self.max_lines))

    def send_status(self, text: str) -> None:
        self._post(html.escape(text, quote=False))

    def check(self, text: str) -> bool:
        return self._post(text)


def _describe(r: requests.Response) -> str:
    try:
        return str(r.json().get("description") or r.text[:300])
    except ValueError:
        return r.text[:300]


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

    def send_status(self, text: str) -> None:
        self._post({"type": "status", "text": text})

    def flush(self, force: bool = False) -> None:
        pass


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

    def send_status(self, text: str) -> None:
        for n in self.notifiers:
            n.send_status(text)

    def flush(self, force: bool = False) -> None:
        for n in self.notifiers:
            n.flush(force)
