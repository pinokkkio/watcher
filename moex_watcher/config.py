from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .markets import DEFAULT_MARKETS, MarketSpec


@dataclass
class Thresholds:
    # «Сильное движение» — абсолютные пороги в %
    day_levels: list[float] = field(default_factory=lambda: [3.0, 5.0, 7.0, 10.0, 15.0])
    fast_window_min: float = 15.0  # окно текущего движения, минут
    fast_move_pct: float = 2.0  # движение от мин./макс. за окно до текущей цены, %
    # «Аномалия» — относительно собственной волатильности и оборота инструмента
    z_day: float = 3.0  # дневное изменение в сигмах дневной волатильности
    z_fast: float = 4.0  # изменение за окно в сигмах (σ_дня · √(окно/сессия))
    z_step: float = 1.0  # повторный алерт при росте z на столько сигм
    rvol: float = 3.0  # оборот к «нормальному на это время» во столько раз
    # Фильтры ликвидности, руб.
    min_value_today: float = 3_000_000
    min_avg_value: float = 10_000_000
    cooldown_min: float = 15.0  # пауза между повторными FAST-алертами по инструменту
    session_start: str = "10:00"  # МСК, для оценки «ожидаемого к этому часу» оборота
    session_end: str = "23:50"

    @property
    def session_minutes(self) -> float:
        h1, m1 = map(int, self.session_start.split(":"))
        h2, m2 = map(int, self.session_end.split(":"))
        return max(60.0, (h2 * 60 + m2) - (h1 * 60 + m1))


DEFAULT_THRESHOLDS: dict[str, Thresholds] = {
    "shares": Thresholds(),
    "bonds": Thresholds(
        day_levels=[1.0, 2.0, 3.0, 5.0, 10.0], fast_move_pct=1.0, z_day=3.5, z_fast=5.0,
        min_value_today=1_000_000, min_avg_value=3_000_000, session_end="18:50",
    ),
    "futures": Thresholds(
        day_levels=[2.0, 4.0, 6.0, 10.0], fast_move_pct=1.5, min_value_today=10_000_000,
        min_avg_value=50_000_000, session_start="09:00",
    ),
}


ALL_SIGNALS = ("STRONG_DAY", "ANOMALY_DAY", "FAST_MOVE", "ANOMALY_FAST", "VOLUME_SPIKE")
# Пока — только текущие движения за окно; дневные сигналы включаются через signals / WATCH_SIGNALS
DEFAULT_SIGNALS = frozenset({"FAST_MOVE", "ANOMALY_FAST"})


def parse_signals(value: str | list[str]) -> frozenset[str]:
    items = value.split(",") if isinstance(value, str) else value
    sig = frozenset(x.strip().upper() for x in items if x.strip())
    bad = sig - set(ALL_SIGNALS)
    if bad:
        raise ValueError(f"Неизвестные сигналы: {sorted(bad)}. Доступны: {', '.join(ALL_SIGNALS)}")
    return sig


@dataclass
class Config:
    markets: list[MarketSpec] = field(default_factory=lambda: [
        DEFAULT_MARKETS[k] for k in ("shares", "ofz", "corp_bonds", "futures")])
    thresholds: dict[str, Thresholds] = field(default_factory=lambda: {
        k: dataclasses.replace(v) for k, v in DEFAULT_THRESHOLDS.items()})
    poll_interval_sec: float = 30.0
    history_days: int = 60
    db_path: str = "data/history.sqlite"
    alerts_log: str | None = "data/alerts.jsonl"
    signals: frozenset[str] = DEFAULT_SIGNALS
    digest_interval_min: float = 0.0  # периодическая сводка самых волатильных, минут; 0 — выкл.
    digest_top: int = 10
    console: bool = True
    telegram_token: str | None = None
    telegram_channel: str | None = None  # канал: @имя, t.me/имя или -100…
    telegram_proxy: str | None = None  # http(s)://host:port — только для запросов к Telegram
    telegram_proxy_user: str | None = None
    telegram_proxy_password: str | None = None
    telegram_proxy_ca: str | None = None  # PEM-сертификат https-прокси, если он самоподписанный
    telegram_api_base: str = "https://api.telegram.org"
    telegram_batch_sec: float = 60.0  # копить сигналы и слать одним сообщением раз в N секунд
    telegram_max_lines: int = 30  # максимум строк-инструментов в одном сообщении
    webhook_url: str | None = None
    iss_login: str | None = None
    iss_password: str | None = None

    def th(self, asset_class: str) -> Thresholds:
        return self.thresholds.get(asset_class) or Thresholds()


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path:
        _apply_file(cfg, path)
    _apply_env(cfg)
    return cfg


# Переменные окружения перекрывают файл — удобно для секретов в Docker.
ENV_MAP = {
    "TELEGRAM_TOKEN": "telegram_token",
    "TELEGRAM_CHAT_ID": "telegram_channel",  # старое имя, оставлено для совместимости
    "TELEGRAM_CHANNEL": "telegram_channel",
    "TELEGRAM_PROXY": "telegram_proxy",
    "TELEGRAM_PROXY_USER": "telegram_proxy_user",
    "TELEGRAM_PROXY_PASSWORD": "telegram_proxy_password",
    "TELEGRAM_PROXY_CA": "telegram_proxy_ca",
    "TELEGRAM_API_BASE": "telegram_api_base",
    "ISS_LOGIN": "iss_login",
    "ISS_PASSWORD": "iss_password",
    "WEBHOOK_URL": "webhook_url",
}


def _apply_env(cfg: Config) -> None:
    for env, attr in ENV_MAP.items():
        v = os.environ.get(env)
        if v:
            setattr(cfg, attr, v)
    for env, attr, typ in (("TELEGRAM_BATCH_SEC", "telegram_batch_sec", float),
                           ("TELEGRAM_MAX_LINES", "telegram_max_lines", int)):
        if os.environ.get(env):
            setattr(cfg, attr, typ(os.environ[env]))
    if os.environ.get("WATCH_SIGNALS"):
        cfg.signals = parse_signals(os.environ["WATCH_SIGNALS"])
    if os.environ.get("DIGEST_INTERVAL_MIN"):
        cfg.digest_interval_min = float(os.environ["DIGEST_INTERVAL_MIN"])
    if os.environ.get("WATCH_MARKETS"):
        keys = [k.strip() for k in os.environ["WATCH_MARKETS"].split(",") if k.strip()]
        bad = [k for k in keys if k not in DEFAULT_MARKETS]
        if bad:
            raise ValueError(f"WATCH_MARKETS: неизвестные рынки {bad}")
        cfg.markets = [DEFAULT_MARKETS[k] for k in keys]


def _apply_file(cfg: Config, path: str | Path) -> None:
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    general = raw.get("general", {})
    for k in ("poll_interval_sec", "history_days", "db_path", "alerts_log", "digest_interval_min",
              "digest_top", "console"):
        if k in general:
            setattr(cfg, k, general[k])
    if "signals" in general:
        cfg.signals = parse_signals(general["signals"])
    if general.get("alerts_log") == "":
        cfg.alerts_log = None

    custom = {k: MarketSpec(key=k, **v) for k, v in raw.get("markets", {}).items()}
    if "watch" in general:
        known = {**DEFAULT_MARKETS, **custom}
        unknown = [k for k in general["watch"] if k not in known]
        if unknown:
            raise ValueError(f"Неизвестные рынки: {unknown}. Доступны: {sorted(known)}")
        cfg.markets = [known[k] for k in general["watch"]]

    for cls, over in raw.get("thresholds", {}).items():
        base = cfg.thresholds.get(cls, Thresholds())
        valid = {f.name for f in dataclasses.fields(Thresholds)}
        bad = set(over) - valid
        if bad:
            raise ValueError(f"Неизвестные параметры thresholds.{cls}: {sorted(bad)}")
        cfg.thresholds[cls] = dataclasses.replace(base, **over)

    tg = raw.get("telegram", {})
    cfg.telegram_token = tg.get("token") or None
    channel = tg.get("channel") or tg.get("chat_id")
    cfg.telegram_channel = str(channel) if channel else None
    cfg.telegram_proxy = tg.get("proxy") or None
    cfg.telegram_proxy_user = tg.get("proxy_user") or None
    cfg.telegram_proxy_password = tg.get("proxy_password") or None
    cfg.telegram_proxy_ca = tg.get("proxy_ca") or None
    cfg.telegram_api_base = tg.get("api_base") or cfg.telegram_api_base
    if "batch_sec" in tg:
        cfg.telegram_batch_sec = float(tg["batch_sec"])
    if "max_lines" in tg:
        cfg.telegram_max_lines = int(tg["max_lines"])
    cfg.webhook_url = raw.get("webhook", {}).get("url") or None
    iss = raw.get("iss", {})
    cfg.iss_login = iss.get("login") or None
    cfg.iss_password = iss.get("password") or None
