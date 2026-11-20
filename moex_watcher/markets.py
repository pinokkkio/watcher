from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketSpec:
    key: str
    asset_class: str  # shares | bonds | futures
    engine: str
    market: str
    board: str
    title: str

    @property
    def ref_price_cols(self) -> tuple[str, ...]:
        if self.asset_class == "futures":
            return ("PREVSETTLEPRICE", "PREVPRICE")
        return ("PREVLEGALCLOSEPRICE", "PREVPRICE", "PREVWAPRICE")

    @property
    def hist_close_cols(self) -> tuple[str, ...]:
        if self.asset_class == "futures":
            return ("SETTLEPRICE", "CLOSE", "WAPRICE")
        return ("LEGALCLOSEPRICE", "CLOSE", "WAPRICE")


# Основные режимы торгов. Можно добавлять свои в конфиге ([markets.<key>]).
DEFAULT_MARKETS: dict[str, MarketSpec] = {
    "shares": MarketSpec("shares", "shares", "stock", "shares", "TQBR", "Акции (Т+)"),
    "etf": MarketSpec("etf", "shares", "stock", "shares", "TQTF", "Фонды (Т+)"),
    "ofz": MarketSpec("ofz", "bonds", "stock", "bonds", "TQOB", "ОФЗ"),
    "corp_bonds": MarketSpec("corp_bonds", "bonds", "stock", "bonds", "TQCB", "Корп. облигации"),
    "futures": MarketSpec("futures", "futures", "futures", "forts", "RFUD", "Фьючерсы FORTS"),
}
