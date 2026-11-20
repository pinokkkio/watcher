from __future__ import annotations

import argparse
import logging
import os
import sys

from datetime import datetime

from . import __version__
from .config import Config, load_config
from .detectors import MSK
from .engine import Watcher
from .history import HistoryStore
from .iss import IssClient
from .markets import DEFAULT_MARKETS
from .notifiers import ConsoleNotifier, JsonlNotifier, MultiNotifier, TelegramNotifier, WebhookNotifier
from .volatility import format_table, rank_historical, rank_intraday


log = logging.getLogger("moex_watcher")


def build_notifier(cfg: Config) -> MultiNotifier:
    ns = []
    if cfg.console:
        ns.append(ConsoleNotifier())
    if cfg.alerts_log:
        ns.append(JsonlNotifier(cfg.alerts_log))
    missing = [n for n, v in (("TELEGRAM_TOKEN", cfg.telegram_token), ("TELEGRAM_CHANNEL", cfg.telegram_channel))
               if not v]
    if missing:
        log.warning("Telegram ВЫКЛЮЧЕН: не задано %s — уведомления идут только в консоль/журнал. "
                    "Проверьте .env и что контейнер пересобран/перезапущен", " и ".join(missing))
    else:
        ns.append(TelegramNotifier(cfg.telegram_token, cfg.telegram_channel, proxy=cfg.telegram_proxy,
                                   proxy_user=cfg.telegram_proxy_user,
                                   proxy_password=cfg.telegram_proxy_password,
                                   proxy_ca=cfg.telegram_proxy_ca, api_base=cfg.telegram_api_base,
                                   batch_sec=cfg.telegram_batch_sec, max_lines=cfg.telegram_max_lines))
    if cfg.webhook_url:
        ns.append(WebhookNotifier(cfg.webhook_url))
    return MultiNotifier(ns)


def startup_message(cfg: Config) -> str:
    now = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    delay = "без задержки (MOEX Passport)" if cfg.iss_login else "с задержкой ~15 мин"
    markets = "\n".join(f"• {m.title} ({m.board})" for m in cfg.markets)
    return (f"🟢 moex-watcher {__version__} запущен — {now} МСК\n\n"
            f"Слежу за рынками:\n{markets}\n\n"
            f"Сигналы:\n{signals_description(cfg)}\n\n"
            f"Опрос каждые {cfg.poll_interval_sec:g} с, данные MOEX ISS {delay}.")


def signals_description(cfg: Config) -> str:
    classes = sorted({m.asset_class for m in cfg.markets}, key=["shares", "bonds", "futures"].index)
    label = {"shares": "акции", "bonds": "облигации", "futures": "фьючерсы"}
    lines = []
    if "FAST_MOVE" in cfg.signals:
        win = cfg.th(classes[0]).fast_window_min if classes else 15
        parts = ", ".join(f"{label[c]} ≥{cfg.th(c).fast_move_pct:g}%" for c in classes)
        lines.append(f"• движение за последние {win:g} мин: {parts}")
    if "ANOMALY_FAST" in cfg.signals:
        lines.append(f"• необычное для бумаги движение за то же окно: ≥{cfg.th('shares').z_fast:g}σ")
    day = {"STRONG_DAY": "сильное движение за день", "ANOMALY_DAY": "аномалия за день (σ)"}
    lines += [f"• {t}" for k, t in day.items() if k in cfg.signals]
    return "\n".join(lines) or "• нет (все сигналы выключены)"


def _select_markets(cfg: Config, markets: str | None) -> None:
    if markets:
        known = {**DEFAULT_MARKETS, **{m.key: m for m in cfg.markets}}
        keys = [k.strip() for k in markets.split(",") if k.strip()]
        bad = [k for k in keys if k not in known]
        if bad:
            raise SystemExit(f"Неизвестные рынки: {bad}. Доступны: {', '.join(sorted(known))}")
        cfg.markets = [known[k] for k in keys]


def cmd_watch(args: argparse.Namespace, cfg: Config) -> int:
    notifier = build_notifier(cfg)
    if args.test_notify:
        tg = next((n for n in notifier.notifiers if isinstance(n, TelegramNotifier)), None)
        if tg is None:
            print("Telegram не настроен: задайте TELEGRAM_TOKEN и TELEGRAM_CHANNEL")
            return 1
        ok = tg.verify() and tg.check("moex-watcher: проверка канала уведомлений ✅")
        print(f"Telegram: сообщение опубликовано в {tg.chat_id}" if ok
              else "Telegram: отправить не удалось, см. лог")
        return 0 if ok else 1
    client = IssClient(login=cfg.iss_login, password=cfg.iss_password)
    w = Watcher(cfg, client, HistoryStore(cfg.db_path), notifier)
    log.info("Отслеживаю: %s", ", ".join(m.board for m in cfg.markets))
    tg = next((n for n in notifier.notifiers if isinstance(n, TelegramNotifier)), None)
    if tg is not None and not tg.verify():
        log.error("Telegram: проверка не пройдена — сообщения в канал, скорее всего, не дойдут (см. выше)")
    notifier.send_status(startup_message(cfg))
    w.run(once=args.once)
    return 0


def cmd_volatile(args: argparse.Namespace, cfg: Config) -> int:
    client = IssClient(login=cfg.iss_login, password=cfg.iss_password)
    store = HistoryStore(cfg.db_path)
    days = args.days or cfg.history_days
    for spec in cfg.markets:
        store.ensure(spec, client, days)
        base = store.baselines(spec.board, days, min_obs=min(10, max(2, days - 1)))
        quotes = client.snapshot(spec) if args.mode != "hist" else []
        names = {q.secid: q.name for q in quotes}
        min_value = args.min_value or 0.0
        if args.mode == "hist":
            rows = rank_historical(base, spec.key, names, min_avg_value=min_value, top=args.top)
            title = f"\n{spec.title} ({spec.board}): топ-{args.top} по исторической волатильности за {days} дн."
        else:
            rows = rank_intraday(quotes, {spec.board: base}, min_value_today=min_value, top=args.top,
                                 by=args.mode)
            label = {"range": "дневному диапазону", "move": "изменению за день", "z": "изменению в сигмах"}
            title = f"\n{spec.title} ({spec.board}): топ-{args.top} сегодня по {label[args.mode]}"
        print(format_table(rows, title))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="moex-watcher", description="Сильные и аномальные движения на Мосбирже")
    p.add_argument("-c", "--config", help="путь к config.toml")
    p.add_argument("-m", "--markets", help=f"рынки через запятую: {', '.join(DEFAULT_MARKETS)}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="непрерывный мониторинг с уведомлениями")
    w.add_argument("--once", action="store_true", help="один цикл опроса и выход")
    w.add_argument("--test-notify", action="store_true", help="отправить тестовое сообщение и выйти")
    w.set_defaults(func=cmd_watch)

    v = sub.add_parser("volatile", help="самые волатильные инструменты")
    v.add_argument("--mode", choices=["range", "move", "z", "hist"], default="range",
                   help="range — дневной диапазон, move — |изменение за день|, z — в сигмах, "
                        "hist — историческая волатильность (по умолчанию range)")
    v.add_argument("--days", type=int, help="глубина истории, торговых дней")
    v.add_argument("--top", type=int, default=20)
    v.add_argument("--min-value", type=float, help="минимальный оборот, руб. (по умолчанию без фильтра)")
    v.set_defaults(func=cmd_volatile)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config or os.environ.get("MOEX_WATCHER_CONFIG") or None)
    _select_markets(cfg, args.markets)
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
