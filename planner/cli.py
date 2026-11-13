"""Точка входа: python -m planner run | brief | sku <id>."""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_cfg(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def cmd_run(args) -> int:
    from . import backtest, data, engine, report

    cfg = load_cfg(args.config)
    data_dir = ROOT / cfg["data"]["dir"]
    new = data.ingest_inbox(ROOT / cfg["data"]["inbox"], data_dir)
    if new:
        print(f"[data] принята новая выгрузка: {new}")
    ds = data.load(data_dir, args.asof or cfg["data"]["asof"])
    print(f"[data] срез {ds.asof}, источник {ds.source}")
    result = engine.run(ds, cfg)
    cutoff = ds.asof.year * 12 + ds.asof.month - 1
    if not args.no_backtest:
        result["backtest"] = backtest.run(ds.sales, ds.stock, ds.receipts, cutoff, cfg)
    paths = report.write_all(result, ROOT, cfg)
    k = result["kpi"]
    print(f"[plan] заказать сейчас: {k['order_now_skus']} SKU / {k['order_now_units']} шт; "
          f"риск дефицита: {k['deficit_critical']} крит + {k['deficit_high']} выс.; "
          f"избыток: {k['excess_skus']} SKU; на пересмотр: {k['low_markup_skus']}")
    for name, p in paths.items():
        print(f"[out] {name}: {p}")
    return 0


def cmd_brief(args) -> int:
    from . import brief, site

    ok, msg = brief.generate(ROOT, load_cfg(args.config))
    print(f"[ai] {'сводка готова' if ok else 'пропущено'}: {msg}")
    print(f"[out] dashboard: {site.build(ROOT)}")
    return 0


def cmd_publish(args) -> int:
    from . import publish

    cfg = load_cfg(args.config)
    print(f"[publish] {publish.publish(ROOT, cfg)}")
    print(f"[notify] {publish.notify(ROOT, cfg)}")
    return 0


def cmd_all(args) -> int:
    rc = cmd_run(args) or cmd_brief(args)
    return rc or cmd_publish(args)


def cmd_sku(args) -> int:
    plan = json.loads((ROOT / "out" / "plan.json").read_text(encoding="utf-8"))
    r = next((s for s in plan["skus"] if s["sku"].lower() == args.sku.lower()), None)
    if not r:
        print("SKU не найден", file=sys.stderr)
        return 1
    r = {k: v for k, v in r.items() if k != "chart"}
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="пересчитать план закупок и собрать дашборд")
    r.add_argument("--asof", help="дата среза YYYY-MM-DD (по умолчанию из config)")
    r.add_argument("--config", type=Path)
    r.add_argument("--no-backtest", action="store_true")
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("brief", help="ИИ-сводка закупщику (claude -p) и пересборка дашборда")
    b.add_argument("--config", type=Path)
    b.set_defaults(fn=cmd_brief)
    a_ = sub.add_parser("all", help="run + brief: полный цикл")
    a_.add_argument("--asof")
    a_.add_argument("--config", type=Path)
    a_.add_argument("--no-backtest", action="store_true")
    a_.set_defaults(fn=cmd_all)
    pb = sub.add_parser("publish", help="выложить дашборд и отправить уведомление")
    pb.add_argument("--config", type=Path)
    pb.set_defaults(fn=cmd_publish)
    s = sub.add_parser("sku", help="показать расчёт по одному SKU")
    s.add_argument("sku")
    s.set_defaults(fn=cmd_sku)
    a = ap.parse_args(argv)
    return a.fn(a)
