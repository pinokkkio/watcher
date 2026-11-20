"""Детекторы сильных и аномальных движений.

Сильное движение — превышение абсолютных порогов в %:
  * STRONG_DAY   — изменение к закрытию прошлого дня пересекло очередной уровень (3/5/7/10…%);
  * FAST_MOVE    — изменение за короткое окно (по умолчанию 5 минут).
Аномалия — движение, необычное для самого инструмента:
  * ANOMALY_DAY  — дневное изменение ≥ z_day сигм его исторической дневной волатильности;
  * ANOMALY_FAST — изменение за окно ≥ z_fast сигм, отмасштабированных на длину окна;
  * VOLUME_SPIKE — оборот в разы выше обычного для этого времени дня.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, Thresholds
from .models import Alert, Baseline, Quote, Reason

MSK = ZoneInfo("Europe/Moscow")

Key = tuple[str, str]


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def session_fraction(now: datetime, th: Thresholds) -> float:
    t = now.astimezone(MSK)
    h, m = map(int, th.session_start.split(":"))
    elapsed = (t.hour * 60 + t.minute) - (h * 60 + m)
    return min(1.0, max(0.0, elapsed / th.session_minutes))


class Detector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.prices: dict[Key, deque[tuple[datetime, float]]] = {}
        self._day: date | None = None
        self._level_hit: dict[tuple[Key, int], int] = {}
        self._z_hit: dict[tuple[Key, int], float] = {}
        self._rvol_hit: dict[Key, float] = {}
        self._fast_at: dict[Key, datetime] = {}

    # --- состояние ----------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        d = now.astimezone(MSK).date()
        if d != self._day:
            self._day = d
            self.prices.clear()
            self._level_hit.clear()
            self._z_hit.clear()
            self._rvol_hit.clear()
            self._fast_at.clear()

    def _record(self, q: Quote, now: datetime, keep: timedelta) -> None:
        dq = self.prices.setdefault(q.key, deque())
        dq.append((now, q.last))  # type: ignore[arg-type]
        while dq and now - dq[0][0] > keep:
            dq.popleft()

    def window_return(self, key: Key, now: datetime, window: timedelta) -> tuple[float, float] | None:
        """(доходность за окно, фактическая длина окна в минутах) или None."""
        dq = self.prices.get(key)
        if not dq or len(dq) < 2:
            return None
        start = next(((t, p) for t, p in dq if now - t <= window), None)
        if start is None:
            return None
        t0, p0 = start
        span = (now - t0).total_seconds() / 60
        if span < window.total_seconds() / 60 * 0.5 or p0 <= 0:
            return None
        return dq[-1][1] / p0 - 1.0, span

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
            window = timedelta(minutes=th.fast_window_min)
            self._record(q, now, keep=window * 2)
            base = baselines.get(q.board, {}).get(q.secid)
            if not self._liquid(q, base, th):
                continue
            reasons = self._check(q, base, th, now, window)
            if reasons:
                alerts.append(Alert(quote=q, reasons=reasons, ts=now))
        return alerts

    @staticmethod
    def _liquid(q: Quote, base: Baseline | None, th: Thresholds) -> bool:
        if (q.value_today or 0) < th.min_value_today:
            return False
        return base is None or base.avg_value >= th.min_avg_value

    def _check(self, q: Quote, base: Baseline | None, th: Thresholds, now: datetime,
               window: timedelta) -> list[Reason]:
        reasons: list[Reason] = []
        key = q.key
        sigma = base.sigma_daily if base and base.sigma_daily > 0 else None

        chg = q.change_pct
        if chg is not None and chg != 0:
            sign = 1 if chg > 0 else -1
            # 1. Сильное дневное движение: пересечение очередного уровня
            lvl = max((i for i, lv in enumerate(th.day_levels) if abs(chg) >= lv), default=-1)
            if lvl >= 0 and lvl > self._level_hit.get((key, sign), -1):
                self._level_hit[(key, sign)] = lvl
                reasons.append(Reason("STRONG_DAY",
                                      f"{_fmt_pct(chg)} за день (порог {th.day_levels[lvl]:g}%)", False))
            # 2. Аномальное дневное движение в сигмах
            if sigma and q.log_change is not None:
                z = q.log_change / sigma
                prev = self._z_hit.get((key, sign))
                if abs(z) >= th.z_day and (prev is None or abs(z) >= prev + th.z_step):
                    self._z_hit[(key, sign)] = abs(z)
                    reasons.append(Reason("ANOMALY_DAY",
                                          f"{z:+.1f}σ за день (обычная дневная σ {sigma * 100:.2f}%)", True))

        # 3/4. Быстрое движение за окно
        wr = self.window_return(key, now, window)
        last_fast = self._fast_at.get(key)
        if wr is not None and (last_fast is None or now - last_fast >= timedelta(minutes=th.cooldown_min)):
            ret, span = wr
            fast: list[Reason] = []
            if abs(ret) * 100 >= th.fast_move_pct:
                fast.append(Reason("FAST_MOVE", f"{_fmt_pct(ret * 100)} за {span:.0f} мин", False))
            if sigma and ret > -1:
                zf = math.log1p(ret) / (sigma * math.sqrt(th.fast_window_min / th.session_minutes))
                if abs(zf) >= th.z_fast:
                    fast.append(Reason("ANOMALY_FAST",
                                       f"{zf:+.1f}σ за {span:.0f} мин ({_fmt_pct(ret * 100)})", True))
            if fast:
                self._fast_at[key] = now
                reasons.extend(fast)

        # 5. Всплеск оборота
        if base and base.avg_value > 0 and q.value_today:
            expected = base.avg_value * max(session_fraction(now, th), 0.1)
            rv = q.value_today / expected
            prev = self._rvol_hit.get(key)
            if rv >= th.rvol and (prev is None or rv >= prev * 2):
                self._rvol_hit[key] = rv
                reasons.append(Reason("VOLUME_SPIKE",
                                      f"оборот ×{rv:.1f} к норме ({q.value_today / 1e6:,.0f} млн ₽)", True))
        return reasons
