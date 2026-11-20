"""Рейтинги самых волатильных инструментов."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Baseline, Quote


@dataclass(slots=True)
class VolRow:
    market: str
    secid: str
    name: str
    ann_vol_pct: float | None  # историческая годовая волатильность
    atr_pct: float | None  # средний дневной диапазон, %
    avg_value_mln: float | None
    change_pct: float | None = None  # текущий день
    range_pct: float | None = None  # текущий дневной диапазон
    z_day: float | None = None  # текущее изменение в сигмах
    score: float = 0.0


def rank_historical(baselines: dict[str, Baseline], market: str, names: dict[str, str] | None = None,
                    min_avg_value: float = 0.0, top: int = 20) -> list[VolRow]:
    names = names or {}
    rows = [
        VolRow(market, b.secid, names.get(b.secid, b.secid), b.ann_vol_pct, b.atr_pct, b.avg_value / 1e6,
               score=b.ann_vol_pct)
        for b in baselines.values() if b.avg_value >= min_avg_value
    ]
    rows.sort(key=lambda r: r.score, reverse=True)
    return rows[:top]


def rank_intraday(quotes: list[Quote], baselines: dict[str, dict[str, Baseline]],
                  min_value_today: float = 0.0, top: int = 20, by: str = "range") -> list[VolRow]:
    """Самые волатильные сегодня.

    by="range" — дневной диапазон (high-low), by="move" — |изменение за день|,
    by="z" — изменение в сигмах собственной волатильности (самые «необычные»)."""
    rows: list[VolRow] = []
    for q in quotes:
        if q.last is None or (q.value_today or 0) < min_value_today or q.change_pct is None:
            continue
        b = baselines.get(q.board, {}).get(q.secid)
        z = q.log_change / b.sigma_daily if b and b.sigma_daily > 0 and q.log_change is not None else None
        row = VolRow(q.market, q.secid, q.name, b.ann_vol_pct if b else None, b.atr_pct if b else None,
                     (q.value_today or 0) / 1e6, q.change_pct, q.range_pct, z)
        if by == "move":
            row.score = abs(q.change_pct)
        elif by == "z":
            if z is None:
                continue
            row.score = abs(z)
        else:
            row.score = q.range_pct or 0.0
        rows.append(row)
    rows.sort(key=lambda r: r.score, reverse=True)
    return rows[:top]


def _f(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def format_table(rows: list[VolRow], title: str = "") -> str:
    head = f"{'#':>3} {'Тикер':<14} {'Название':<20} {'Изм.д.%':>8} {'Диап.%':>7} {'z':>6} " \
           f"{'Вол.год%':>9} {'ATR%':>6} {'Оборот,млн':>11}"
    lines = [title, head, "-" * len(head)] if title else [head, "-" * len(head)]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>3} {r.secid:<14.14} {r.name:<20.20} {_f(r.change_pct, '+8.2f'):>8} {_f(r.range_pct, '7.2f'):>7} "
            f"{_f(r.z_day, '+6.1f'):>6} {_f(r.ann_vol_pct, '9.1f'):>9} {_f(r.atr_pct, '6.2f'):>6} "
            f"{_f(r.avg_value_mln, '11,.1f'):>11}"
        )
    return "\n".join(lines)


def format_digest(rows: list[VolRow], title: str) -> str:
    lines = [title]
    for i, r in enumerate(rows, 1):
        z = f", {r.z_day:+.1f}σ" if r.z_day is not None else ""
        lines.append(f"{i}. {r.secid} ({r.name}): {_f(r.change_pct, '+.2f')}%, "
                     f"диапазон {_f(r.range_pct, '.2f')}%{z}")
    return "\n".join(lines)
