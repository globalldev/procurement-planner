"""Сборка плана закупок по всему ассортименту."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .data import Dataset
from .forecast import censored_months, fit_sku, mi, mi_str, prepare_sales, seasonality
from .policy import Calendar, lead_days, pack_size, plan_sku, stock_now, transit_for

MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _r(x, n=0):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n) if n else int(round(float(x)))


def supplier_stats(po: pd.DataFrame) -> dict:
    done = po[po["status"] != "open"]
    out = {}
    for s, g in done.groupby("supplier_id"):
        ordered = g["qty_ordered"].sum()
        out[s] = {
            "pos": int(len(g)),
            "fill_rate": _r(g["qty_received"].sum() / ordered * 100, 1) if ordered else None,
            "zero_received_share": _r((g["qty_received"] == 0).mean() * 100, 1),
        }
    return out


def run(ds: Dataset, cfg: dict) -> dict:
    asof = ds.asof
    cutoff = asof.year * 12 + asof.month - 1          # текущий (неполный) месяц
    sales = prepare_sales(ds.sales)
    full = sales[sales["mi"] < cutoff]
    stock = ds.stock.copy()
    stock["mi"] = stock["month"].map(mi)
    stock = stock[stock["mi"] <= cutoff]
    snap_mi = int(stock["mi"].max()) if len(stock) else cutoff

    fcfg, pol, eco = cfg["forecast"], cfg["policy"], cfg["economics"]
    season = seasonality(full, cutoff, fcfg)
    cens = censored_months(stock, full, ds.receipts)
    cal = Calendar(asof, pol["horizon_days"])
    sup_lead = ds.suppliers.set_index("supplier_id")["median_planned_lead_days"].to_dict()
    sup_stats = supplier_stats(ds.po)

    sales_by = dict(tuple(sales.groupby("sku_id")))
    stock_by = dict(tuple(stock.groupby("sku_id")))
    po_by = dict(tuple(ds.po.groupby("sku_id")))
    rc_by = dict(tuple(ds.receipts.groupby("sku_id")))
    empty_s = sales.iloc[0:0]
    empty_st = stock.iloc[0:0]
    empty_po = ds.po.iloc[0:0]
    empty_rc = ds.receipts.iloc[0:0]

    # --- ABC по прокси оборота (штуки × ценовой дециль), XYZ по вариативности
    units12 = full[full["mi"] >= cutoff - 12].groupby("sku_id")["y"].sum()
    dim = ds.sku.set_index("sku_id")
    proxy = (units12 * dim["price_decile"].reindex(units12.index).fillna(5)).sort_values(ascending=False)
    proxy = proxy[proxy > 0]
    cum = proxy.cumsum() / proxy.sum() if proxy.sum() else proxy
    abc = {k: ("A" if v <= 0.8 else "B" if v <= 0.95 else "C") for k, v in cum.items()}

    hist_from = cutoff - 24
    month_labels_hist = [mi_str(m) for m in range(hist_from, cutoff + 1)]
    fut_months = list(range(cutoff, cutoff + 15))
    month_end_days = []
    for m in fut_months:
        idx = np.nonzero(cal.day_mi == m)[0]
        month_end_days.append(int(idx[-1]) if len(idx) else None)

    rows = []
    for sku, d in dim.iterrows():
        g = sales_by.get(sku, empty_s)
        st = stock_by.get(sku, empty_st)
        po = po_by.get(sku, empty_po)
        rc = rc_by.get(sku, empty_rc)
        cat = d["category"]
        sup = d["supplier_id"]
        if pd.isna(sup):
            # в справочнике пусто — берём поставщика из последнего заказа/прихода
            src = po.sort_values("order_date")["supplier_id"].dropna() if len(po) else pd.Series(dtype=object)
            if not len(src) and len(rc):
                src = rc.sort_values("receipt_date")["supplier_id"].dropna()
            sup = src.iat[-1] if len(src) else "не указан"
        u12 = float(units12.get(sku, 0.0))
        partial = float(g.loc[g["mi"] >= cutoff, "y"].sum())
        sold_months = g.loc[g["y"] > 0, "mi"]
        last_sale = int(sold_months.max()) if len(sold_months) else None
        markup = d["avg_markup_pct_12m"]
        markup = None if pd.isna(markup) else float(markup)
        s0 = stock_now(g, st, rc, snap_mi)
        transit = transit_for(po, asof, cfg)
        L, L_src = lead_days(sku, sup, d["supply_type"], ds.po, sup_lead, cfg)
        pack = pack_size(po["qty_ordered"].tolist())
        idx = season.sku.get(sku, season.category.get(cat, np.ones(12)))
        fc = fit_sku(g[g["mi"] < cutoff], idx, cutoff, cens, sku, fcfg)
        first_mi = int(g["mi"].min()) if len(g) else None
        age = (cutoff - first_mi) if first_mi is not None else 0

        dead = u12 == 0 and partial == 0
        cls = abc.get(sku, "C")
        p = plan_sku(cal=cal, fc=fc, stock0=s0, transit=transit, L=L, abc=cls, pack=pack, cfg=cfg)

        flags = []
        low_markup = markup is not None and markup < eco["min_markup_pct"]
        no_cost = markup is None and not dead
        if no_cost:
            flags.append("нет себестоимости в ERP — наценка неизвестна, проверить до заказа")
        if d["supply_type"] == "unknown":
            flags.append("нет истории поставок — поставщик/плечо не подтверждены")
        if age < fcfg["new_sku_months"] and not dead:
            flags.append("новинка — мало истории, прогноз осторожный")
        for t in transit:
            if t["overdue_days"] > 0:
                flags.append(("PO %s просрочен на %d дн. — " % (t["po_id"], t["overdue_days"]))
                             + ("учтён с приходом через %d дн." % pol["overdue_grace_days"] if t["counted"]
                                else "не учтён, проверить статус в ERP"))
        in_stock = st.loc[st["stock_qty"] > 2, "mi"]
        out_months = (snap_mi - int(in_stock.max())) if len(in_stock) else None
        if not dead and s0 <= 0 and out_months and out_months >= 3 and not any(t["counted"] for t in transit):
            flags.append(f"нет в наличии {out_months} мес. и нет заказа — подтвердить, что позиция не выведена")
        if fc.lower_bound and not dead:
            flags.append("весь последний год товара не было — спрос занижен; если позиция в ассортименте, объём оценить вручную")
        elif fc.months_censored:
            flags.append(f"{fc.months_censored} мес. дефицита исключены из оценки спроса")
        if pd.isna(d["weight_kg"]):
            flags.append("нет веса — не войдёт в расчёт контейнера")

        orders = p["orders"]
        first = orders[0] if orders else None
        win = pol["order_now_window_days"]
        if dead:
            status = "dead"
        elif low_markup:
            status = "low_markup"
        elif first and first["order_day"] <= win:
            status = "order_now"
        elif first and first["order_day"] <= 60:
            status = "order_soon"
        elif first:
            status = "planned"
        else:
            status = "ok"

        w = None if pd.isna(d["weight_kg"]) else float(d["weight_kg"])
        cover = p["stockout_with"]
        sup_info = sup_stats.get(sup, {})

        fc12 = [fc.month(m) for m in range(cutoff, cutoff + 12)]
        hist_y = []
        gi = g.set_index("mi")["y"]
        sti = st.set_index("mi")["stock_qty"]
        for m in range(hist_from, cutoff + 1):
            hist_y.append(_r(gi.get(m)) if m in gi.index else None)
        hist_stock = []
        for m in range(hist_from, cutoff + 1):
            if m < (stock["mi"].min() if len(stock) else cutoff):
                hist_stock.append(None)
            else:
                hist_stock.append(_r(sti.get(m, 0)))
        proj_base = [(_r(p["inv_base"][e]) if e is not None else None) for e in month_end_days]
        proj_plan = [(_r(p["inv_plan"][e]) if e is not None else None) for e in month_end_days]

        # пояснение расчёта
        peak = int(np.argmax(idx))
        explain = []
        if dead:
            explain.append(f"Продаж не было {eco['dead_months']}+ мес. — мёртвый ассортимент.")
            if s0 > 0:
                explain.append(f"На складе {int(s0)} шт — кандидат на распродажу/списание.")
        else:
            basis = (f"все {fc.months_used} мес. окна товара не было — спрос оценён по продажам снизу"
                     if fc.lower_bound else f"оценка по {fc.months_used} мес. с наличием"
                     + (f", {fc.months_censored} мес. дефицита исключены" if fc.months_censored else ""))
            explain.append(
                f"Спрос ≈ {fc.level:.1f} шт в средний месяц; сезонный пик — {MONTHS_RU[peak]} "
                f"(×{idx[peak]:.1f}); {basis}.")
            explain.append(f"Остаток сейчас ≈ {int(s0)} шт; в пути {int(sum(t['qty'] for t in transit if t['counted']))} шт.")
            explain.append(
                "Запасов хватит до " + (cal.date(cover).strftime("%d.%m.%Y") if cover is not None else "конца горизонта")
                + ("" if cover is None else f" ({cover} дн.)") + ".")
            explain.append(f"Плечо {L} дн. ({L_src}, вкл. до {cfg['lead_time']['buffer_days']} дн. запаса на задержку, "
                           f"не более {cfg['lead_time']['max_days']}); "
                           f"страховой запас {p['ss']:.0f} шт в средний месяц, по сезону ×0,1–2 (уровень сервиса класса {cls}).")
            if first:
                a = first["arrival_day"]
                explain.append(
                    f"Заказ {cal.date(first['order_day']).strftime('%d.%m.%Y')} придёт ≈ {cal.date(a).strftime('%d.%m.%Y')}; "
                    f"спрос на {pol['review_days']} дн. после прихода ≈ {p['demand'][a:a + pol['review_days']].sum():.0f} шт "
                    f"+ страховой − ожидаемый остаток → {first['qty']} шт (с округлением до кратности партии).")
            if p["deficit"]:
                dd = p["deficit"]
                explain.append(
                    f"Риск: товар закончится {cal.date(dd['stockout_day']).strftime('%d.%m.%Y')}, новый заказ успеет только "
                    f"≈ {cal.date(dd['stockout_day'] + dd['gap_days']).strftime('%d.%m.%Y')} — потеря ≈ {dd['lost_units']} шт. "
                    "Закрывать срочной/локальной поставкой или перераспределением.")
                if status not in ("order_now", "low_markup") and float(np.max(idx)) > 1.5:
                    explain.append("Морской заказ сегодня пришёл бы уже после пика сезона, поэтому он не рекомендуется — "
                                   "следующий заказ рассчитан к следующему сезону.")
            if no_cost:
                explain.append("Себестоимость в ERP не заведена — наценку не проверить; в плане оставлен, но с пометкой.")
            if low_markup:
                explain.append(f"Наценка {markup:.0f}% < {eco['min_markup_pct']}% — в автозакупку не идёт, нужен пересмотр цены.")

        rows.append({
            "sku": sku, "category": cat, "supplier": sup, "supply_type": d["supply_type"], "brand": d["brand_id"],
            "abc": cls if not dead else "-", "xyz": ("X" if fc.level and fc.sigma / fc.level < 0.5 else
                                                       "Y" if fc.level and fc.sigma / fc.level < 1 else "Z") if fc.level else "-",
            "status": status, "markup": markup, "no_cost": bool(no_cost), "price_decile": _r(d["price_decile"]) if pd.notna(d["price_decile"]) else None,
            "weight_kg": w, "units_12m": _r(u12), "last_sale": mi_str(last_sale) if last_sale is not None else None,
            "level": _r(fc.level, 1), "sigma": _r(fc.sigma, 1), "ss": _r(p["ss"]),
            "stock": _r(s0), "transit": transit, "transit_qty": _r(sum(t["qty"] for t in transit if t["counted"])),
            "lead_days": L, "lead_src": L_src, "pack": pack,
            "cover_days": cover, "stockout_date": cal.date(cover).isoformat() if cover is not None else None,
            "cover_days_no_transit": p["stockout_without"],
            "demand_next_lr": _r(p["demand"][: L + pol["review_days"]].sum()),
            "orders": [{
                "order_date": cal.date(o["order_day"]).isoformat(), "arrival_date": cal.date(o["arrival_day"]).isoformat(),
                "order_day": o["order_day"], "qty": o["qty"], "weight_kg": _r(o["qty"] * w, 1) if w else None,
            } for o in orders],
            "order_now_qty": first["qty"] if status in ("order_now",) else 0,
            "next_order_date": cal.date(first["order_day"]).isoformat() if first else None,
            "deficit": ({**p["deficit"], "stockout_date": cal.date(p["deficit"]["stockout_day"]).isoformat()}
                        if p["deficit"] and not dead else None),
            "excess": ({**p["excess"], "months": _r(p["on_hand_all"] / max(fc.level, 1e-9), 1) if fc.level else None}
                       if p["excess"] else ({"excess_units": _r(p["on_hand_all"]), "months": None} if dead and p["on_hand_all"] > 0 else None)),
            "lost_units_12m_plan": _r(p["lost_plan"]),
            "supplier_fill_rate": sup_info.get("fill_rate"),
            "flags": flags, "explain": explain,
            "chart": {"hist_y": hist_y, "hist_stock": hist_stock, "fc12": [_r(x, 1) for x in fc12],
                      "proj_base": proj_base, "proj_plan": proj_plan},
            "season_idx": [round(float(x), 2) for x in idx],
        })

    # --- агрегаты
    active = [r for r in rows if r["status"] != "dead"]
    plan_rows = [r for r in rows if r["status"] not in ("dead", "low_markup")]
    now = [r for r in plan_rows if r["status"] == "order_now"]

    by_supplier = {}
    for r in now:
        s = by_supplier.setdefault(r["supplier"], {"supplier": r["supplier"], "skus": 0, "units": 0, "weight_kg": 0.0,
                                                   "no_weight": 0, "categories": set(),
                                                   "fill_rate": sup_stats.get(r["supplier"], {}).get("fill_rate")})
        o = r["orders"][0]
        s["skus"] += 1
        s["units"] += o["qty"]
        if o["weight_kg"] is not None:
            s["weight_kg"] += o["weight_kg"]
        else:
            s["no_weight"] += 1
        s["categories"].add(r["category"])
    suppliers = sorted(({**v, "weight_kg": round(v["weight_kg"], 1), "categories": sorted(v["categories"])}
                        for v in by_supplier.values()), key=lambda x: -x["units"])

    cal_months = {}
    for r in plan_rows:
        for o in r["orders"]:
            if o["order_day"] > 365:
                continue
            k = o["order_date"][:7]
            m = cal_months.setdefault(k, {"month": k, "skus": 0, "units": 0, "weight_kg": 0.0, "by_cat": {}})
            m["skus"] += 1
            m["units"] += o["qty"]
            m["weight_kg"] += o["weight_kg"] or 0
            m["by_cat"][r["category"]] = m["by_cat"].get(r["category"], 0) + o["qty"]
    calendar_rows = [{**v, "weight_kg": round(v["weight_kg"], 1)} for _, v in sorted(cal_months.items())]

    deficit = [r for r in active if r["deficit"]]
    excess = [r for r in rows if r["excess"]]
    kpi = {
        "skus_total": len(rows), "skus_active": len(active), "skus_dead": len(rows) - len(active),
        "dead_with_stock": sum(1 for r in rows if r["status"] == "dead" and (r["stock"] or 0) > 0),
        "dead_stock_units": sum(r["stock"] or 0 for r in rows if r["status"] == "dead"),
        "order_now_skus": len(now), "order_now_units": sum(r["order_now_qty"] for r in now),
        "order_now_weight": round(sum(r["orders"][0]["weight_kg"] or 0 for r in now), 1),
        "order_soon_skus": sum(1 for r in plan_rows if r["status"] == "order_soon"),
        "planned_skus": sum(1 for r in plan_rows if r["status"] == "planned"),
        "ok_skus": sum(1 for r in plan_rows if r["status"] == "ok"),
        "deficit_critical": sum(1 for r in deficit if r["deficit"]["severity"] == "critical"),
        "deficit_high": sum(1 for r in deficit if r["deficit"]["severity"] == "high"),
        "deficit_lost_units": sum(r["deficit"]["lost_units"] for r in deficit),
        "excess_skus": sum(1 for r in excess if r["status"] != "dead"),
        "excess_units": sum(r["excess"]["excess_units"] for r in excess if r["status"] != "dead"),
        "low_markup_skus": sum(1 for r in rows if r["status"] == "low_markup"),
        "transit_units": sum(r["transit_qty"] or 0 for r in rows),
        "transit_future_units": int(sum(t["qty"] for r in rows for t in r["transit"] if t["overdue_days"] == 0)),
        "transit_late_counted_units": int(sum(t["qty"] for r in rows for t in r["transit"] if 0 < t["overdue_days"] and t["counted"])),
        "transit_stale_units": int(sum(t["qty"] for r in rows for t in r["transit"] if not t["counted"])),
        "no_cost_skus": sum(1 for r in rows if r.get("no_cost")),
        "no_cost_order_now": sum(1 for r in rows if r.get("no_cost") and r["status"] == "order_now"),
        "overdue_po": sum(1 for r in rows for t in r["transit"] if t["overdue_days"] > 0),
    }

    active_ids = {r["sku"] for r in active}
    last12 = full[(full["mi"] >= cutoff - 12) & full["sku_id"].isin(active_ids)]
    share_out = 100.0 * sum((k, m) in cens for k, m in zip(last12["sku_id"], last12["mi"])) / max(len(last12), 1)
    po_done = ds.po[ds.po["status"] != "open"]
    weird_transit = [r["sku"] for r in rows if r["transit_qty"] and r["level"] is not None
                     and r["transit_qty"] > 24 * max(r["level"], 0.01)]
    dq = [
        {"title": "Вес единицы заполнен", "value": f"{int(dim['weight_kg'].notna().sum())} из {len(dim)} SKU",
         "note": "Контейнеры по весу/объёму не спланировать — нужен справочник веса и м³."},
        {"title": "Пустая наценка ≠ мёртвый товар",
         "value": f"{int((dim['avg_markup_pct_12m'].isna() & dim.index.isin([r['sku'] for r in rows if r['status'] != 'dead'])).sum())} SKU",
         "note": f"В описании выгрузки пустая наценка = нет продаж 12 мес. ({int(dim['avg_markup_pct_12m'].isna().sum())} SKU), "
                 f"но часть из них продаётся — в ERP просто нет себестоимости. Мёртвыми по факту продаж считаем {len(rows) - len(active)} SKU; "
                 "продающиеся без себестоимости оставлены в плане с пометкой «наценка неизвестна»."},
        {"title": "Нет поставщика в справочнике", "value": f"{int(dim['supplier_id'].isna().sum())} SKU",
         "note": "Подставлен поставщик из последнего заказа/прихода, где он есть."},
        {"title": "Закрытые PO без единого прихода", "value": f"{int((po_done['qty_received'] == 0).sum())} из {len(po_done)}",
         "note": "Приходы в ERP не привязаны к PO — исполняемость поставщиков по PO занижена, ориентир — receipts."},
        {"title": "Просроченные открытые PO", "value": f"{kpi['overdue_po']}",
         "note": f"Старше {pol['overdue_trust_days']} дн. не учитываются в запасе — проверить и закрыть в ERP."},
        {"title": "В пути больше 2 лет спроса", "value": f"{len(weird_transit)} SKU",
         "note": "Проверить привязку заказа к SKU: " + ", ".join(weird_transit[:6]) if weird_transit else "Нет."},
        {"title": "Месяцы без товара у активных SKU", "value": f"{share_out:.0f}% за 12 мес.",
         "note": "Товар кончался — продажи там занижены, такие месяцы исключены из оценки спроса."},
    ]

    return {
        "data_quality": dq,
        "meta": {
            "asof": asof.isoformat(), "source": ds.source, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "current_month": mi_str(cutoff), "snapshot_month": mi_str(snap_mi),
            "hist_labels": month_labels_hist, "fut_labels": [mi_str(m) for m in fut_months],
            "params": {k: cfg[k] for k in ("economics", "lead_time", "policy", "forecast")},
        },
        "kpi": kpi, "skus": rows, "suppliers_now": suppliers, "calendar": calendar_rows,
        "season": {c: [round(float(x), 2) for x in v] for c, v in season.category.items()},
        "supplier_stats": sup_stats,
    }
