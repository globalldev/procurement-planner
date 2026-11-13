"""Выходные артефакты: plan.json, plan.xlsx, история запусков, сравнение, сводка для ИИ, сайт."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

CAT_RU = {
    "sanitary_faucets": "Смесители", "trimmer_blades_accessories": "Лески и ножи", "string_trimmers": "Триммеры",
    "grain_mills": "Зернодробилки", "concrete_mixers": "Бетономешалки", "space_heaters": "Обогреватели",
}
SEV_RU = {"critical": "критично", "high": "высокий"}

STATUS_RU = {
    "order_now": "Заказать сейчас", "order_soon": "Заказать ≤60 дн.", "planned": "Запланирован",
    "ok": "Запаса хватает", "low_markup": "Пересмотр цены", "dead": "Мёртвый ассортимент",
}


def clean(o):
    """NaN/inf → null: JSON.parse в браузере падает на NaN."""
    import math
    if isinstance(o, float):
        return None if not math.isfinite(o) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def _snapshot(result: dict) -> dict:
    return {
        "asof": result["meta"]["asof"], "generated_at": result["meta"]["generated_at"], "kpi": result["kpi"],
        "skus": {r["sku"]: {"status": r["status"], "qty": r["order_now_qty"], "next": r["next_order_date"],
                            "deficit": (r["deficit"] or {}).get("severity"), "stock": r["stock"]}
                 for r in result["skus"]},
    }


def diff(prev: dict | None, cur: dict) -> dict:
    if not prev:
        return {"first_run": True}
    p, c = prev["skus"], cur["skus"]
    out = {"first_run": False, "prev_generated_at": prev["generated_at"], "prev_asof": prev["asof"],
           "new_order_now": [], "dropped_order_now": [], "qty_changed": [], "new_deficit": [], "resolved_deficit": [],
           "kpi_delta": {k: cur["kpi"][k] - prev["kpi"].get(k, 0) for k in cur["kpi"]
                         if isinstance(cur["kpi"][k], (int, float)) and cur["kpi"][k] != prev["kpi"].get(k, 0)}}
    for sku, r in c.items():
        o = p.get(sku)
        if not o:
            continue
        if r["status"] == "order_now" and o["status"] != "order_now":
            out["new_order_now"].append({"sku": sku, "qty": r["qty"], "was": STATUS_RU.get(o["status"], o["status"])})
        if o["status"] == "order_now" and r["status"] != "order_now":
            out["dropped_order_now"].append({"sku": sku, "now": STATUS_RU.get(r["status"], r["status"])})
        if r["status"] == o["status"] == "order_now" and o["qty"] and abs(r["qty"] - o["qty"]) / o["qty"] >= 0.15:
            out["qty_changed"].append({"sku": sku, "was": o["qty"], "now": r["qty"]})
        if r["deficit"] and not o["deficit"]:
            out["new_deficit"].append({"sku": sku, "severity": r["deficit"]})
        if o["deficit"] and not r["deficit"]:
            out["resolved_deficit"].append({"sku": sku})
    return out


def _xlsx(result: dict, path: Path) -> None:
    rows = result["skus"]

    def base(r):
        return {"SKU": r["sku"], "Категория": CAT_RU.get(r["category"], r["category"]), "Поставщик": r["supplier"], "ABC": r["abc"], "XYZ": r["xyz"],
                "Статус": STATUS_RU[r["status"]], "Остаток": r["stock"], "В пути": r["transit_qty"],
                "Спрос/мес (ср.)": r["level"], "Хватит до": r["stockout_date"] or "—", "Дней покрытия": r["cover_days"],
                "Плечо, дн": r["lead_days"], "Страховой": r["ss"],
                "Наценка %": r["markup"] if r["markup"] is not None else "нет себестоимости"}

    order_now = [{**base(r), "Заказать, шт": r["orders"][0]["qty"], "Кратность": r["pack"],
                  "Приход ≈": r["orders"][0]["arrival_date"], "Вес, кг": r["orders"][0]["weight_kg"],
                  "Риск дефицита": SEV_RU.get((r["deficit"] or {}).get("severity"), "нет"), "Почему": " ".join(r["explain"])}
                 for r in rows if r["status"] == "order_now"]
    cal = [{"SKU": r["sku"], "Категория": CAT_RU.get(r["category"], r["category"]), "Поставщик": r["supplier"], "Дата заказа": o["order_date"],
            "Приход ≈": o["arrival_date"], "Кол-во": o["qty"], "Вес, кг": o["weight_kg"]}
           for r in rows if r["status"] not in ("dead", "low_markup") for o in r["orders"]]
    deficit = [{**base(r), "Дефицит с": r["deficit"]["stockout_date"], "Дней без товара": r["deficit"]["gap_days"],
                "Потеря продаж, шт": r["deficit"]["lost_units"], "Уровень": SEV_RU[r["deficit"]["severity"]]}
               for r in rows if r["deficit"]]
    excess = [{**base(r), "Избыток, шт": r["excess"]["excess_units"], "Запас, мес": r["excess"]["months"]}
              for r in rows if r["excess"] and r["status"] != "dead"]
    review = [{**base(r), "Заказали бы, шт": r["orders"][0]["qty"] if r["orders"] else 0}
              for r in rows if r["status"] == "low_markup"]
    no_cost = [{**base(r), "След. заказ": r["next_order_date"] or "—",
                "Объём": r["orders"][0]["qty"] if r["orders"] else 0} for r in rows if r.get("no_cost")]
    dead = [{"SKU": r["sku"], "Категория": CAT_RU.get(r["category"], r["category"]), "Последняя продажа": r["last_sale"], "Остаток": r["stock"]}
            for r in rows if r["status"] == "dead"]
    allr = [{**base(r), "След. заказ": r["next_order_date"] or "—", "Флаги": "; ".join(r["flags"])} for r in rows]

    sup_rows = [{"Поставщик": s["supplier"], "SKU к заказу": s["skus"], "Штук": s["units"],
                 "Исполнение PO по ERP, %": s["fill_rate"],
                 "Категории": ", ".join(CAT_RU.get(c, c) for c in s["categories"])} for s in result["suppliers_now"]]

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, data in [("Заказать сейчас", order_now), ("Календарь заказов", cal), ("Риск дефицита", deficit),
                           ("Избыток", excess), ("Пересмотр цены", review), ("Нет себестоимости", no_cost),
                           ("Мёртвый ассортимент", dead),
                           ("Поставщики", sup_rows), ("Все SKU", allr)]:
            df = pd.DataFrame(data)
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:50] if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)


def ai_summary(result: dict, d: dict) -> dict:
    """Компактный контекст для ИИ-сводки (без поштучных графиков)."""
    rows = result["skus"]
    top_now = sorted((r for r in rows if r["status"] == "order_now"),
                     key=lambda r: ((r["deficit"] or {}).get("lost_units", 0), r["order_now_qty"]), reverse=True)[:25]
    crit = sorted((r for r in rows if r["deficit"]), key=lambda r: -r["deficit"]["lost_units"])[:15]
    exc = sorted((r for r in rows if r["excess"] and r["status"] != "dead"), key=lambda r: -r["excess"]["excess_units"])[:10]
    pick = lambda r, extra: {"sku": r["sku"], "cat": r["category"], "sup": r["supplier"], "abc": r["abc"],
                             "stock": r["stock"], "transit": r["transit_qty"], "level": r["level"],
                             "cover_until": r["stockout_date"], **extra}
    return {
        "asof": result["meta"]["asof"], "params": result["meta"]["params"],
        # вес заполнен у единиц SKU — суммарный вес вводит в заблуждение, в сводку не отдаём
        "kpi": {k: v for k, v in result["kpi"].items() if k != "order_now_weight"},
        "order_now_top": [pick(r, {"qty": r["order_now_qty"], "arrival": r["orders"][0]["arrival_date"],
                                   "deficit": r["deficit"]}) for r in top_now],
        "deficit_top": [pick(r, {"deficit": r["deficit"]}) for r in crit],
        "excess_top": [pick(r, {"excess": r["excess"]}) for r in exc],
        "suppliers_now": result["suppliers_now"][:10],
        "calendar": [{k: v for k, v in m.items() if k != "by_cat"} for m in result["calendar"]],
        "backtest": {k: result.get("backtest", {}).get(k) for k in ("period", "total", "stockout_share")},
        "overdue_po": [{"sku": r["sku"], **t} for r in rows for t in r["transit"] if t["overdue_days"] > 0],
        "data_quality": result.get("data_quality", []),
        "changes": d,
    }


def _n(x) -> str:
    return f"{x:,.0f}".replace(",", " ")


def fallback_brief(result: dict, d: dict) -> str:
    k = result["kpi"]
    m = result["meta"]
    sups = ", ".join(x["supplier"] for x in result["suppliers_now"][:3])
    lines = [
        f"**Срез {m['asof']}.** Заказать сейчас — **{k['order_now_skus']} SKU, {_n(k['order_now_units'])} шт**; "
        f"основной объём у поставщиков {sups}.",
        f"Риск дефицита до прихода нового заказа: **{k['deficit_critical']} критичных и {k['deficit_high']} высоких** позиций, "
        f"ожидаемая потеря ≈ {_n(k['deficit_lost_units'])} шт — морем их не закрыть, нужны локальные/срочные поставки.",
        f"Избыточный запас: {k['excess_skus']} SKU, {_n(k['excess_units'])} шт сверх {m['params']['policy']['excess_months']} мес. спроса.",
        f"На пересмотр цены (наценка < {m['params']['economics']['min_markup_pct']}%): {k['low_markup_skus']} SKU. "
        f"Мёртвый ассортимент: {k['skus_dead']} SKU, из них с остатком {k['dead_with_stock']}.",
    ]
    if k["overdue_po"]:
        lines.append(f"Просроченных открытых PO: {k['overdue_po']} — проверить статус у поставщиков.")
    if not d.get("first_run"):
        lines.append(f"С прошлого запуска: +{len(d['new_order_now'])} в «заказать сейчас», "
                     f"−{len(d['dropped_order_now'])} ушли, {len(d['qty_changed'])} с изменением объёма, "
                     f"{len(d['new_deficit'])} новых рисков дефицита.")
    return "\n\n".join(lines)


def write_all(result: dict, root: Path, cfg: dict) -> dict:
    out = root / "out"
    hist = out / "history"
    hist.mkdir(parents=True, exist_ok=True)
    snaps = sorted(hist.glob("*.json"))
    prev = json.loads(snaps[-1].read_text(encoding="utf-8")) if snaps else None
    snap = _snapshot(result)
    d = diff(prev, snap)
    result["diff"] = d

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (hist / f"{stamp}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    result = clean(result)
    (out / "plan.json").write_text(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False), encoding="utf-8")
    (out / "ai_context.json").write_text(json.dumps(clean(ai_summary(result, d)), ensure_ascii=False, indent=1,
                                                    default=str, allow_nan=False), encoding="utf-8")
    brief_path = out / "ai_brief.md"
    # сводка детерминированная по умолчанию; ИИ-версию пишет `python -m planner brief`
    (out / "brief_fallback.md").write_text(fallback_brief(result, d), encoding="utf-8")
    xlsx = out / "plan.xlsx"
    _xlsx(result, xlsx)

    from .site import build
    site = build(root)
    return {"plan.json": out / "plan.json", "plan.xlsx": xlsx, "ai_context": out / "ai_context.json",
            "brief": brief_path, "dashboard": site}
