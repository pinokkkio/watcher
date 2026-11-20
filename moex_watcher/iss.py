"""Клиент публичного API Московской биржи (ISS)."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Iterable

import requests

from .markets import MarketSpec
from .models import Quote

log = logging.getLogger(__name__)

ISS_BASE = "https://iss.moex.com/iss"
PASSPORT_URL = "https://passport.moex.com/authenticate"


def table(payload: dict[str, Any], block: str) -> list[dict[str, Any]]:
    """Превращает блок ISS {columns: [...], data: [[...]]} в список словарей."""
    b = payload.get(block) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row)) for row in b.get("data") or []]


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _first(row: dict[str, Any], cols: Iterable[str], positive: bool = True) -> float | None:
    for c in cols:
        v = _num(row.get(c))
        if v is not None and (not positive or v > 0):
            return v
    return None


def parse_snapshot(spec: MarketSpec, payload: dict[str, Any], ts: datetime | None = None) -> list[Quote]:
    secs = {r["SECID"]: r for r in table(payload, "securities") if r.get("SECID")}
    quotes: list[Quote] = []
    for md in table(payload, "marketdata"):
        secid = md.get("SECID")
        if not secid:
            continue
        if md.get("BOARDID") and md["BOARDID"] != spec.board:
            continue
        s = secs.get(secid, {})
        merged = {**s, **{k: v for k, v in md.items() if v is not None}}
        nt = _num(merged.get("NUMTRADES"))
        quotes.append(
            Quote(
                market=spec.key,
                asset_class=spec.asset_class,
                board=spec.board,
                secid=secid,
                name=str(s.get("SHORTNAME") or s.get("SECNAME") or secid),
                last=_first(merged, ("LAST",)),
                prev=_first(s, spec.ref_price_cols) or _first(md, spec.ref_price_cols),
                open=_first(md, ("OPEN",)),
                high=_first(md, ("HIGH",)),
                low=_first(md, ("LOW",)),
                value_today=_num(md.get("VALTODAY")),
                num_trades=int(nt) if nt is not None else None,
                ts=ts,
            )
        )
    return quotes


class IssClient:
    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 3,
        session: requests.Session | None = None,
        login: str | None = None,
        password: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "moex-watcher/0.1")
        if login and password:
            self._authenticate(login, password)

    def _authenticate(self, login: str, password: str) -> None:
        """Авторизация в MOEX Passport: при наличии подписки ISS отдаёт данные без задержки."""
        r = self.session.get(PASSPORT_URL, auth=(login, password), timeout=self.timeout)
        r.raise_for_status()
        if "MicexPassportCert" not in self.session.cookies:
            log.warning("MOEX Passport: cookie не получена, данные будут с задержкой")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {"iss.meta": "off", **(params or {})}
        url = f"{ISS_BASE}{path}"
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                if attempt == self.retries:
                    raise
                log.warning("ISS %s: %s (попытка %d/%d)", path, e, attempt, self.retries)
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def snapshot(self, spec: MarketSpec) -> list[Quote]:
        path = f"/engines/{spec.engine}/markets/{spec.market}/boards/{spec.board}/securities.json"
        payload = self.get(path, {"iss.only": "securities,marketdata"})
        return parse_snapshot(spec, payload, ts=datetime.now().astimezone())

    def history_day(self, spec: MarketSpec, day: date) -> list[dict[str, Any]]:
        """Итоги торгов по всем инструментам режима за один день (с пагинацией)."""
        path = f"/history/engines/{spec.engine}/markets/{spec.market}/boards/{spec.board}/securities.json"
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = self.get(path, {"date": day.isoformat(), "start": start, "iss.only": "history,history.cursor"})
            page = table(payload, "history")
            rows.extend(page)
            cur = table(payload, "history.cursor")
            if cur:
                total, size = int(cur[0].get("TOTAL") or 0), int(cur[0].get("PAGESIZE") or 100)
                start += size
                if start >= total:
                    break
            elif not page:
                break
            else:
                start += len(page)
            if not page:
                break
        return rows
