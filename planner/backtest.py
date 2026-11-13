"""Проверка прогноза на истории: «прогноз из прошлого» против факта.

Обрезаем данные на N мес. назад, строим прогноз тем же кодом и сравниваем помесячно с фактом за следующие 12 мес.
Бенчмарки: среднее за 12 мес. (как часто планируют вручную) и «тот же месяц год назад».

Сравниваем только там, где продажи показывают спрос: месяц выпадает из проверки, если товар кончился или на складе
лежали остатки меньше заданной доли обычного спроса этого месяца ([backtest] min_avail_share). Без второго условия
месяц с 1–10 шт на складе при спросе в сотни считался «товар был» и записывал в ошибку прогноза обычный дефицит.

Одна точка отсчёта — один год и его случайности, поэтому считаем несколько ([backtest] origins_months_back):
первая — основная (с разбивкой по категориям), остальные показывают, устойчив ли результат.
"""
from __future__ import annotations

import pandas as pd

from .forecast import censored_months, fit_sku, mi, mi_str, prepare_sales, seasonality

COLS = ["actual", "model", "avg12", "same_month_ly"]
MIN_SNAPSHOT_MONTHS = 6   # меньше снимков остатков до точки отсчёта — дефицитные месяцы обучения не распознать


def _wape(g, col) -> float:
    return float((g[col] - g["actual"]).abs().sum() / max(g["actual"].sum(), 1e-9) * 100)


def _bias(g, col) -> float:
    return float((g[col].sum() - g["actual"].sum()) / max(g["actual"].sum(), 1e-9) * 100)


def _block(g: pd.DataFrame, cut: int) -> dict:
    # точность на горизонте закупки: сумма за 3 мес. (квартал) на SKU
    g2 = g.assign(q=(g["mi"] - cut) // 3).groupby(["sku", "q"])[COLS].sum()
    return {
        "wape_model": round(_wape(g, "model"), 1), "wape_avg12": round(_wape(g, "avg12"), 1),
        "wape_ly": round(_wape(g, "same_month_ly"), 1),
        "wape_q_model": round(_wape(g2, "model"), 1), "wape_q_avg12": round(_wape(g2, "avg12"), 1),
        "wape_q_ly": round(_wape(g2, "same_month_ly"), 1),
        "bias_model": round(_bias(g, "model"), 1), "bias_avg12": round(_bias(g, "avg12"), 1),
        "bias_ly": round(_bias(g, "same_month_ly"), 1), "skus": int(g["sku"].nunique()),
    }


def _origin(sales: pd.DataFrame, stock_all: pd.DataFrame, receipts: pd.DataFrame, cutoff_now: int, cfg: dict,
            back: int, detail: bool) -> dict | None:
    bt = cfg.get("backtest", {})
    cut = cutoff_now - back
    end = min(cut + 12, cutoff_now)
    full = sales[sales["mi"] < end]
    train = full[full["mi"] < cut]
    stock = stock_all[stock_all["mi"] < cut]
    if train.empty or stock["mi"].nunique() < MIN_SNAPSHOT_MONTHS:
        return None
    season = seasonality(train, cut, cfg["forecast"])
    rc = receipts[receipts["receipt_date"] < pd.Timestamp(cut // 12, cut % 12 + 1, 1)]
    cens = censored_months(stock, train, rc)

    # месяцы теста, по которым прогноз не проверить: товар кончился или лежали остатки меньше доли обычного спроса
    test = full[full["mi"] >= cut]
    season_end = seasonality(full, end, cfg["forecast"])
    ref = full[full["mi"] >= end - bt.get("ref_months", 24)].groupby("sku_id")["y"].mean().to_dict()
    usual = {(s, m): ref.get(s, 0.0) * season_end.sku.get(s, season_end.category[c])[m % 12]
             for s, c, m in zip(test["sku_id"], test["category"], test["mi"])}
    rc_end = receipts[receipts["receipt_date"] < pd.Timestamp(end // 12, end % 12 + 1, 1)]
    cens_test = censored_months(stock_all[stock_all["mi"] < end], test, rc_end, usual, bt.get("min_avail_share", 0.0))

    actual_by = {s: g.set_index("mi")["y"] for s, g in test.groupby("sku_id")}
    empty = pd.Series(dtype=float)
    recs = []
    for sku, g in train.groupby("sku_id"):
        last12 = g[g["mi"] >= cut - 12]
        if last12["y"].sum() == 0:
            continue
        cat = g["category"].iat[0]
        idx = season.sku.get(sku, season.category[cat])
        fc = fit_sku(g, idx, cut, cens, sku, cfg["forecast"])
        actual = actual_by.get(sku, empty)
        ly = g.set_index("mi")["y"]
        naive = last12["y"].mean()
        for m in range(cut, end):
            recs.append({"sku": sku, "category": cat, "mi": m, "actual": float(actual.get(m, 0.0)),
                         "model": fc.month(m), "avg12": naive, "same_month_ly": float(ly.get(m - 12, 0.0)),
                         "stockout": (sku, m) in cens_test})
    if not recs:
        return None
    df = pd.DataFrame(recs)
    clean = df[~df["stockout"]]
    out = {"back": back, "period": [mi_str(cut), mi_str(end - 1)], "total": _block(clean, cut),
           "stockout_share": round(float(df["stockout"].mean() * 100), 1)}
    if not detail:
        return out
    out["total_incl_stockouts"] = _block(df, cut)
    out["by_category"] = {c: _block(g, cut) for c, g in clean.groupby("category")}
    monthly = clean.groupby("mi")[["actual", "model", "avg12"]].sum()
    out["monthly"] = [{"month": mi_str(m), **{k: round(float(v)) for k, v in r.items()}} for m, r in monthly.iterrows()]
    out["cat_monthly"] = {
        c: [{k: round(float(v)) for k, v in r.items()} for _, r in g.groupby("mi")[["actual", "model", "avg12"]].sum().iterrows()]
        for c, g in clean.groupby("category")}
    return out


def run(sales_raw: pd.DataFrame, stock_raw: pd.DataFrame, receipts: pd.DataFrame, cutoff_now: int, cfg: dict,
        back: int | None = None) -> dict:
    sales = prepare_sales(sales_raw)
    sales = sales[sales["mi"] < cutoff_now]
    stock_all = stock_raw.assign(mi=stock_raw["month"].map(mi))
    backs = [back] if back else list(cfg.get("backtest", {}).get("origins_months_back", [12]))
    out = _origin(sales, stock_all, receipts, cutoff_now, cfg, backs[0], detail=True)
    if out is None:
        return {}
    origins = [{"back": out["back"], "period": out["period"], "stockout_share": out["stockout_share"], **out["total"]}]
    for b in backs[1:]:
        o = _origin(sales, stock_all, receipts, cutoff_now, cfg, b, detail=False)
        if o:
            origins.append({"back": o["back"], "period": o["period"], "stockout_share": o["stockout_share"], **o["total"]})
    out["origins"] = origins
    out["min_avail_share"] = cfg.get("backtest", {}).get("min_avail_share", 0.0)
    return out
