"""Дневные сигналы (по умолчанию выключены, см. Config.signals).

  * STRONG_DAY  — изменение к закрытию прошлого дня пересекло очередной уровень (3/5/7/10…%);
  * ANOMALY_DAY — дневное изменение ≥ z_day сигм исторической дневной волатильности бумаги.

Движения за 15 минут (FAST_MOVE) ищет scanner.MoveScanner по минутным свечам.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Config, Thresholds
from .models import Alert, Baseline, Quote, Reason

MSK = ZoneInfo("Europe/Moscow")

Key = tuple[str, str]


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


class Detector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._day: date | None = None
        self._level_hit: dict[tuple[Key, int], int] = {}
        self._z_hit: dict[tuple[Key, int], float] = {}

    # --- состояние ----------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        d = now.astimezone(MSK).date()
        if d != self._day:
            self._day = d
            self._level_hit.clear()
            self._z_hit.clear()

    # --- основной метод -----------------------------------------------------
    def process(
        self,
        quotes: list[Quote],
        baselines: dict[str, dict[str, Baseline]],
        now: datetime | None = None,
    ) -> list[Alert]:
        now = now or datetime.now(MSK)
        self._roll_day(now)
        alerts: list[Alert] = []
        for q in quotes:
            if q.last is None or q.last <= 0:
                continue
            th = self.cfg.th(q.asset_class)
            base = baselines.get(q.board, {}).get(q.secid)
            reasons = self._check(q, base, th)
            if reasons:
                alerts.append(Alert(quote=q, reasons=reasons, ts=now))
        return alerts

    def _check(self, q: Quote, base: Baseline | None, th: Thresholds) -> list[Reason]:
        reasons: list[Reason] = []
        key = q.key
        sigma = base.sigma_daily if base and base.sigma_daily > 0 else None

        enabled = self.cfg.signals
        chg = q.change_pct
        if chg is not None and chg != 0 and (enabled & {"STRONG_DAY", "ANOMALY_DAY"}):
            sign = 1 if chg > 0 else -1
            # 1. Сильное дневное движение: пересечение очередного уровня
            lvl = max((i for i, lv in enumerate(th.day_levels) if abs(chg) >= lv), default=-1)
            if "STRONG_DAY" in enabled and lvl >= 0 and lvl > self._level_hit.get((key, sign), -1):
                self._level_hit[(key, sign)] = lvl
                reasons.append(Reason("STRONG_DAY",
                                      f"{_fmt_pct(chg)} за день (порог {th.day_levels[lvl]:g}%)", False,
                                      f"{'≥' if sign > 0 else '≤'}{sign * th.day_levels[lvl]:+g}% за день"))
            # 2. Аномальное дневное движение в сигмах
            if "ANOMALY_DAY" in enabled and sigma and q.log_change is not None:
                z = q.log_change / sigma
                prev = self._z_hit.get((key, sign))
                if abs(z) >= th.z_day and (prev is None or abs(z) >= prev + th.z_step):
                    self._z_hit[(key, sign)] = abs(z)
                    reasons.append(Reason("ANOMALY_DAY",
                                          f"{z:+.1f}σ за день (обычная дневная σ {sigma * 100:.2f}%)", True,
                                          f"{z:+.1f}σ за день"))

        return reasons
