"""Локальный кэш дневной истории (SQLite) и расчёт базовой статистики инструментов."""
from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from .markets import MarketSpec
from .models import Baseline

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    board TEXT NOT NULL, secid TEXT NOT NULL, d TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, value REAL,
    PRIMARY KEY (board, secid, d)
);
CREATE TABLE IF NOT EXISTS fetched (board TEXT NOT NULL, d TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (board, d));
"""


class HistorySource(Protocol):
    def history_day(self, spec: MarketSpec, day: date) -> list[dict[str, Any]]: ...


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class HistoryStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    # --- загрузка -----------------------------------------------------------
    def save_day(self, spec: MarketSpec, day: date, rows: list[dict[str, Any]], final: bool = True) -> int:
        recs = []
        for r in rows:
            if r.get("BOARDID") and r["BOARDID"] != spec.board:
                continue
            close = next((_f(r.get(c)) for c in spec.hist_close_cols if _f(r.get(c))), None)
            if not r.get("SECID") or close is None:
                continue
            recs.append((spec.board, r["SECID"], day.isoformat(), _f(r.get("OPEN")), _f(r.get("HIGH")),
                         _f(r.get("LOW")), close, _f(r.get("VALUE")) or 0.0))
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?)", recs)
            if final or recs:
                self.db.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)", (spec.board, day.isoformat(), len(recs)))
        return len(recs)

    def ensure(self, spec: MarketSpec, source: HistorySource, days: int, today: date | None = None) -> int:
        """Догружает недостающие торговые дни, пока не наберётся `days` дней с данными.

        Возвращает число загруженных из сети дат."""
        today = today or date.today()
        fetched = dict(self.db.execute("SELECT d, n FROM fetched WHERE board=?", (spec.board,)).fetchall())
        trading = 0
        downloads = 0
        failures = 0
        d = today - timedelta(days=1)
        limit = today - timedelta(days=int(days * 1.6) + 14)
        while trading < days + 1 and d >= limit:
            key = d.isoformat()
            if key in fetched:
                n = fetched[key]
            else:
                try:
                    rows = source.history_day(spec, d)
                except Exception as e:  # noqa: BLE001
                    log.warning("История %s за %s не загружена: %s", spec.board, key, e)
                    failures += 1
                    if failures >= 3:
                        log.error("История %s: ISS недоступен, работаю без базовой статистики", spec.board)
                        break
                    d -= timedelta(days=1)
                    continue
                failures = 0
                # пустой результат за последние дни не кэшируем — итоги могли ещё не опубликовать
                n = self.save_day(spec, d, rows, final=(today - d).days > 3)
                downloads += 1
            if n:
                trading += 1
            d -= timedelta(days=1)
        if downloads:
            log.info("История %s: загружено %d дат, торговых дней в окне: %d", spec.board, downloads, trading)
        return downloads

    # --- статистика ---------------------------------------------------------
    def series(self, board: str, days: int) -> dict[str, list[tuple[str, float | None, float | None, float, float]]]:
        dates = [r[0] for r in self.db.execute(
            "SELECT d FROM fetched WHERE board=? AND n>0 ORDER BY d DESC LIMIT ?", (board, days + 1))]
        if not dates:
            return {}
        out: dict[str, list] = defaultdict(list)
        for secid, d, hi, lo, close, value in self.db.execute(
            "SELECT secid, d, high, low, close, value FROM daily WHERE board=? AND d>=? ORDER BY secid, d",
            (board, min(dates)),
        ):
            out[secid].append((d, hi, lo, close, value))
        return out

    def baselines(self, board: str, days: int, min_obs: int = 10) -> dict[str, Baseline]:
        res: dict[str, Baseline] = {}
        for secid, rows in self.series(board, days).items():
            rets = [math.log(b[3] / a[3]) for a, b in zip(rows, rows[1:]) if a[3] and b[3]]
            if len(rets) < min_obs:
                continue
            ranges = [(hi - lo) / c * 100 for _, hi, lo, c, _ in rows if hi and lo and c]
            res[secid] = Baseline(
                secid=secid,
                n=len(rets),
                sigma_daily=statistics.stdev(rets),
                avg_value=statistics.fmean(r[4] for r in rows),
                atr_pct=statistics.fmean(ranges) if ranges else 0.0,
            )
        return res
