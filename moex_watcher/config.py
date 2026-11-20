from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .markets import DEFAULT_MARKETS, MarketSpec


@dataclass
class Thresholds:
    # «Сильное движение» — абсолютные пороги в %
    day_levels: list[float] = field(default_factory=lambda: [3.0, 5.0, 7.0, 10.0, 15.0])
    fast_window_min: float = 5.0  # окно для быстрых движений, минут
    fast_move_pct: float = 1.5  # изменение за окно, %
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
        day_levels=[1.0, 2.0, 3.0, 5.0, 10.0], fast_move_pct=0.7, z_day=3.5, z_fast=5.0,
        min_value_today=1_000_000, min_avg_value=3_000_000, session_end="18:50",
    ),
    "futures": Thresholds(
        day_levels=[2.0, 4.0, 6.0, 10.0], fast_move_pct=1.0, min_value_today=10_000_000,
        min_avg_value=50_000_000, session_start="09:00",
    ),
}


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
    digest_interval_min: float = 60.0  # периодическая сводка самых волатильных; 0 — выкл.
    digest_top: int = 10
    console: bool = True
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    iss_login: str | None = None
    iss_password: str | None = None

    def th(self, asset_class: str) -> Thresholds:
        return self.thresholds.get(asset_class) or Thresholds()


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if not path:
        return cfg
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    general = raw.get("general", {})
    for k in ("poll_interval_sec", "history_days", "db_path", "alerts_log", "digest_interval_min",
              "digest_top", "console"):
        if k in general:
            setattr(cfg, k, general[k])
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
    cfg.telegram_chat_id = str(tg["chat_id"]) if tg.get("chat_id") else None
    cfg.webhook_url = raw.get("webhook", {}).get("url") or None
    iss = raw.get("iss", {})
    cfg.iss_login = iss.get("login") or None
    cfg.iss_password = iss.get("password") or None
    return cfg
