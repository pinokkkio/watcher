"""Главный цикл: опрос рынков, детекция, уведомления, периодическая сводка."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Protocol

from .config import Config
from .detectors import MSK, Detector
from .history import HistoryStore
from .markets import MarketSpec
from .models import Alert, Baseline, Quote
from .notifiers import Notifier
from .volatility import format_digest, rank_intraday

log = logging.getLogger(__name__)


class MarketSource(Protocol):
    def snapshot(self, spec: MarketSpec) -> list[Quote]: ...
    def history_day(self, spec: MarketSpec, day: date): ...


class Watcher:
    def __init__(self, cfg: Config, source: MarketSource, store: HistoryStore, notifier: Notifier) -> None:
        self.cfg = cfg
        self.source = source
        self.store = store
        self.notifier = notifier
        self.detector = Detector(cfg)
        self.baselines: dict[str, dict[str, Baseline]] = {}
        self.quotes: dict[str, list[Quote]] = {}
        self._baseline_day: date | None = None
        self._last_digest: float = time.monotonic()

    def refresh_baselines(self, force: bool = False) -> None:
        today = datetime.now(MSK).date()
        if not force and self._baseline_day == today:
            return
        for spec in self.cfg.markets:
            self.store.ensure(spec, self.source, self.cfg.history_days, today=today)
            self.baselines[spec.board] = self.store.baselines(spec.board, self.cfg.history_days)
            log.info("%s: базовая статистика по %d инструментам", spec.board, len(self.baselines[spec.board]))
        self._baseline_day = today

    def tick(self, now: datetime | None = None) -> list[Alert]:
        alerts: list[Alert] = []
        for spec in self.cfg.markets:
            try:
                quotes = self.source.snapshot(spec)
            except Exception as e:  # noqa: BLE001
                log.warning("Снимок %s не получен: %s", spec.board, e)
                continue
            self.quotes[spec.key] = quotes
            alerts.extend(self.detector.process(quotes, self.baselines, now=now))
        if alerts:
            self.notifier.send_alerts(alerts)
        return alerts

    def digest(self) -> str:
        parts = []
        for spec in self.cfg.markets:
            rows = rank_intraday(self.quotes.get(spec.key, []), self.baselines,
                                 top=self.cfg.digest_top, by="range")
            if rows:
                parts.append(format_digest(rows, f"Самые волатильные сегодня — {spec.title}:"))
        return "\n\n".join(parts)

    def run(self, once: bool = False) -> None:
        try:
            self._loop(once)
        finally:
            self.notifier.flush(force=True)  # не теряем накопленное при остановке

    def _loop(self, once: bool) -> None:
        self.refresh_baselines(force=True)
        while True:
            started = time.monotonic()
            try:
                self.refresh_baselines()
                self.tick()
                if self.cfg.digest_interval_min > 0 and \
                        started - self._last_digest >= self.cfg.digest_interval_min * 60:
                    self._last_digest = started
                    text = self.digest()
                    if text:
                        self.notifier.send_text(text)
                self.notifier.flush()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле опроса")
            if once:
                return
            time.sleep(max(1.0, self.cfg.poll_interval_sec - (time.monotonic() - started)))
