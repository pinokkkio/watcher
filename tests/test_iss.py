from datetime import date

from moex_watcher.iss import IssClient, parse_snapshot
from moex_watcher.markets import DEFAULT_MARKETS

SHARES_PAYLOAD = {
    "securities": {
        "columns": ["SECID", "BOARDID", "SHORTNAME", "PREVPRICE", "PREVLEGALCLOSEPRICE", "LOTSIZE"],
        "data": [
            ["SBER", "TQBR", "Сбербанк", 300.0, 301.0, 10],
            ["GAZP", "TQBR", "ГАЗПРОМ ао", 130.0, None, 10],
            ["DEAD", "TQBR", "Нет торгов", 10.0, 10.0, 1],
        ],
    },
    "marketdata": {
        "columns": ["SECID", "BOARDID", "LAST", "OPEN", "HIGH", "LOW", "VALTODAY", "NUMTRADES"],
        "data": [
            ["SBER", "TQBR", 316.05, 301.5, 317.0, 300.0, 9.5e9, 120000],
            ["GAZP", "TQBR", 126.1, 130.0, 131.0, 125.0, 2e9, 50000],
            ["DEAD", "TQBR", None, None, None, None, 0, 0],
        ],
    },
}

FUT_PAYLOAD = {
    "securities": {"columns": ["SECID", "BOARDID", "SHORTNAME", "PREVSETTLEPRICE", "PREVPRICE"],
                   "data": [["SiZ6", "RFUD", "Si-12.26", 90000, 89000]]},
    "marketdata": {"columns": ["SECID", "BOARDID", "LAST", "HIGH", "LOW", "VALTODAY"],
                   "data": [["SiZ6", "RFUD", 91800, 92000, 89900, 5e10]]},
}


def test_parse_shares_snapshot():
    qs = {q.secid: q for q in parse_snapshot(DEFAULT_MARKETS["shares"], SHARES_PAYLOAD)}
    assert qs["SBER"].prev == 301.0  # официальное закрытие приоритетнее PREVPRICE
    assert round(qs["SBER"].change_pct, 2) == 5.0
    assert qs["GAZP"].prev == 130.0  # fallback на PREVPRICE
    assert qs["DEAD"].last is None and qs["DEAD"].change_pct is None
    assert qs["SBER"].name == "Сбербанк" and qs["SBER"].num_trades == 120000


def test_parse_futures_uses_settle():
    (q,) = parse_snapshot(DEFAULT_MARKETS["futures"], FUT_PAYLOAD)
    assert q.prev == 90000 and round(q.change_pct, 2) == 2.0
    assert round(q.range_pct, 3) == round(2100 / 90000 * 100, 3)


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        page = self.pages[len(self.calls) - 1]

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return page

        return R()


def test_history_pagination():
    cols = ["BOARDID", "SECID", "CLOSE", "VALUE"]
    pages = [
        {"history": {"columns": cols, "data": [["TQBR", f"S{i}", 1, 1] for i in range(100)]},
         "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 150, 100]]}},
        {"history": {"columns": cols, "data": [["TQBR", f"S{i}", 1, 1] for i in range(100, 150)]},
         "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[100, 150, 100]]}},
    ]
    s = FakeSession(pages)
    rows = IssClient(session=s).history_day(DEFAULT_MARKETS["shares"], date(2026, 11, 19))
    assert len(rows) == 150
    assert [c["start"] for c in s.calls] == [0, 100]
    assert s.calls[0]["date"] == "2026-11-19"
