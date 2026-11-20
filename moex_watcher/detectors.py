"""Детекторы сильных и аномальных движений.

Какие сигналы активны, задаёт Config.signals (по умолчанию только движения за 15 минут).

Сильное движение — превышение абсолютных порогов в %:
  * STRONG_DAY   — изменение к закрытию прошлого дня пересекло очередной уровень (3/5/7/10…%);
  * FAST_MOVE    — текущее движение: от минимума/максимума за окно (15 мин) до последней цены.
Аномалия — движение, необычное для самого инструмента:
  * ANOMALY_DAY  — дневное изменение ≥ z_day сигм его исторической дневной волатильности;
  * ANOMALY_FAST — изменение за окно ≥ z_fast сигм, отмасштабированных на длину окна;
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


class Detector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.prices: dict[Key, deque[tuple[datetime, float]]] = {}
        self._day: date | None = None
        self._level_hit: dict[tuple[Key, int], int] = {}
        self._z_hit: dict[tuple[Key, int], float] = {}
        self._fast_at: dict[tuple[Key, int], datetime] = {}  # пауза отдельно для роста и падения
        self._fast_size: dict[tuple[Key, int], float] = {}

    # --- состояние ----------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        d = now.astimezone(MSK).date()
        if d != self._day:
            self._day = d
            self.prices.clear()
            self._level_hit.clear()
            self._z_hit.clear()
            self._fast_at.clear()
            self._fast_size.clear()

    def _record(self, q: Quote, now: datetime, keep: timedelta) -> None:
        dq = self.prices.setdefault(q.key, deque())
        dq.append((now, q.last))  # type: ignore[arg-type]
        while dq and now - dq[0][0] > keep:
            dq.popleft()

    def window_move(self, key: Key, now: datetime, window: timedelta) -> tuple[float, float] | None:
        """Текущее движение: от минимума (рост) или максимума (падение) за окно до последней цены.

        Возвращает (доходность, минут от экстремума до сейчас) — берётся большее по модулю."""
        dq = self.prices.get(key)
        if not dq or len(dq) < 2:
            return None
        pts = [(t, p) for t, p in dq if now - t <= window and p > 0]
        if len(pts) < 2:
            return None
        last = pts[-1][1]
        t_lo, lo = min(pts, key=lambda x: (x[1], -x[0].timestamp()))  # самый поздний минимум
        t_hi, hi = max(pts, key=lambda x: (x[1], x[0].timestamp()))  # самый поздний максимум
        up, down = last / lo - 1.0, last / hi - 1.0
        ret, t0 = (up, t_lo) if abs(up) >= abs(down) else (down, t_hi)
        if ret == 0:
            return None
        return ret, (now - t0).total_seconds() / 60

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
            reasons = self._check(q, base, th, now, window)
            if reasons:
                alerts.append(Alert(quote=q, reasons=reasons, ts=now))
        return alerts

    def _check(self, q: Quote, base: Baseline | None, th: Thresholds, now: datetime,
               window: timedelta) -> list[Reason]:
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

        # 3/4. Текущее движение за окно (по умолчанию 15 мин)
        wr = self.window_move(key, now, window)
        if wr is not None and (enabled & {"FAST_MOVE", "ANOMALY_FAST"}):
            ret, span = wr
            mins = f"{span:.0f} мин" if span >= 1 else "<1 мин"
            fast: list[Reason] = []
            if "FAST_MOVE" in enabled and abs(ret) * 100 >= th.fast_move_pct:
                fast.append(Reason("FAST_MOVE", f"{_fmt_pct(ret * 100)} за {mins}", False,
                                   f"{ret * 100:+.1f}% за {mins}", ret * 100))
            if "ANOMALY_FAST" in enabled and sigma and ret > -1:
                zf = math.log1p(ret) / (sigma * math.sqrt(th.fast_window_min / th.session_minutes))
                if abs(zf) >= th.z_fast:
                    # одна запись в строке: «+2.2% за 4 мин (4.6σ)»; если сработал и порог в %,
                    # σ дописывается к нему, а у аномалии краткая форма пустая
                    combined = f"{ret * 100:+.1f}% за {mins} ({abs(zf):.1f}σ)"
                    if fast:
                        fast[0].short = combined
                    fast.append(Reason("ANOMALY_FAST",
                                       f"{zf:+.1f}σ за {mins} ({_fmt_pct(ret * 100)})", True,
                                       "" if fast else combined, ret * 100))
            dkey = (key, 1 if ret > 0 else -1)
            last_fast = self._fast_at.get(dkey)
            cooled = last_fast is None or now - last_fast >= timedelta(minutes=th.cooldown_min)
            # в паузе — только если движение в ту же сторону усилилось вдвое; разворот не глушится
            grown = abs(ret) >= 2 * self._fast_size.get(dkey, math.inf)
            if fast and (cooled or grown):
                self._fast_at[dkey] = now
                self._fast_size[dkey] = abs(ret)
                reasons.extend(fast)

        return reasons
