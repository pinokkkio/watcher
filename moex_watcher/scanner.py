"""Сильные движения за 15 минут по минутным свечам.

Проверка идёт раз в poll_interval (по умолчанию 15 мин). На каждой проверке по каждой бумаге
ищется самое сильное движение внутри любого окна длиной fast_window_min, которое закончилось
после предыдущей проверки. Движение — от минимума (рост) или максимума (падение) в окне до
цены закрытия минуты. На старте проверяются последние 15 минут доступных данных.

Свечи запрашиваются только по кандидатам: бумагам, у которых дневной диапазон high/low уже
не меньше порога — иначе движения на порог за 15 минут быть не могло.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .config import Config
from .detectors import MSK
from .markets import MarketSpec
from .models import Alert, Candle, Quote, Reason

log = logging.getLogger(__name__)

DATA_LAG = timedelta(minutes=20)  # запас на задержку данных ISS (15 мин) при запросе свечей
MAX_GAP = timedelta(hours=1)  # дольше этого после прошлой проверки не смотрим назад


class CandleSource(Protocol):
    def candles(self, spec: MarketSpec, secid: str, start: datetime) -> list[Candle]: ...


@dataclass(slots=True)
class Move:
    ret: float  # доля, со знаком
    start: datetime  # минута экстремума (начало движения)
    end: datetime  # минута, на закрытии которой зафиксировано движение
    close: float


def strongest_move(candles: list[Candle], window: timedelta, since: datetime,
                   not_before: dict[int, datetime] | None = None) -> Move | None:
    """Самое сильное движение в окне `window`, закончившееся после `since`.

    not_before[+1 | -1] — рост/падение считаются только от экстремумов не раньше этого времени
    (конец уже сообщённого движения в ту же сторону), чтобы не повторять его."""
    candles = sorted(candles, key=lambda c: c.begin)
    nb = not_before or {}
    floor = datetime.min
    best: Move | None = None
    for j, c in enumerate(candles):
        if c.end <= since:
            continue
        lo: Candle | None = None
        hi: Candle | None = None
        i = j
        while i >= 0 and candles[i].begin >= c.end - window:
            k = candles[i]
            if k.begin >= nb.get(1, floor) and (lo is None or k.low < lo.low):
                lo = k
            if k.begin >= nb.get(-1, floor) and (hi is None or k.high > hi.high):
                hi = k
            i -= 1
        for ret, ext in ((c.close / lo.low - 1 if lo else 0.0, lo), (c.close / hi.high - 1 if hi else 0.0, hi)):
            if ext and ret and (best is None or abs(ret) > abs(best.ret)):
                best = Move(ret, ext.begin, c.begin, c.close)
    return best


class MoveScanner:
    def __init__(self, cfg: Config, source: CandleSource) -> None:
        self.cfg = cfg
        self.source = source
        self._prev_check: dict[str, datetime] = {}
        self._seen_until: dict[tuple[str, str], datetime] = {}
        self._reported: dict[tuple[str, str], dict[int, datetime]] = {}  # конец сообщённых движений
        self._day: date | None = None

    @staticmethod
    def candidates(quotes: list[Quote], threshold_pct: float) -> list[Quote]:
        res = [q for q in quotes if q.last and q.high and q.low and q.low > 0
               and (q.high / q.low - 1) * 100 >= threshold_pct]
        return sorted(res, key=lambda q: q.high / q.low, reverse=True)  # type: ignore[operator]

    def scan(self, spec: MarketSpec, quotes: list[Quote], now: datetime | None = None) -> list[Alert]:
        now = (now or datetime.now(MSK)).astimezone(MSK)
        if now.date() != self._day:
            self._day = now.date()
            self._seen_until.clear()
            self._reported.clear()
        th = self.cfg.th(spec.asset_class)
        window = timedelta(minutes=th.fast_window_min)
        prev = self._prev_check.get(spec.board)
        elapsed = min(now - prev, MAX_GAP) if prev else window  # на старте — последние 15 минут
        self._prev_check[spec.board] = now
        start = now.replace(tzinfo=None) - elapsed - window - DATA_LAG

        cands = self.candidates(quotes, th.fast_move_pct)
        if len(cands) > self.cfg.max_candidates:
            log.warning("%s: кандидатов %d, проверяю первые %d по дневному диапазону", spec.board,
                        len(cands), self.cfg.max_candidates)
            cands = cands[: self.cfg.max_candidates]
        log.info("%s: свечи по %d кандидатам (дневной диапазон ≥ %g%%)", spec.board, len(cands),
                 th.fast_move_pct)

        alerts: list[Alert] = []
        for q in cands:
            try:
                candles = self.source.candles(spec, q.secid, start)
            except Exception as e:  # noqa: BLE001
                log.warning("Свечи %s %s не получены: %s", spec.board, q.secid, e)
                continue
            if not candles:
                continue
            last_end = max(c.end for c in candles)
            since = self._seen_until.get(q.key) or (last_end - elapsed)
            self._seen_until[q.key] = last_end
            reported = self._reported.setdefault(q.key, {})
            mv = strongest_move(candles, window, since, reported)
            if mv is None or abs(mv.ret) * 100 < th.fast_move_pct:
                continue
            reported[1 if mv.ret > 0 else -1] = mv.end
            mins = max(1, round((mv.end - mv.start).total_seconds() / 60))  # минимум — одна минута
            when = f"{mv.start:%H:%M}–{mv.end:%H:%M}"
            text = f"{mv.ret * 100:+.1f}% за {mins} мин ({when})"
            quote = dataclasses.replace(q, last=mv.close)
            alerts.append(Alert(quote=quote, ts=now,
                                reasons=[Reason("FAST_MOVE", text, False, text, mv.ret * 100)]))
        return alerts
