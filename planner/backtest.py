"""Проверка прогноза на истории: «прогноз из прошлого» против факта.

Обрезаем данные на 12 мес. назад, строим прогноз тем же кодом и сравниваем
помесячно с фактом. Бенчмарки: среднее за 12 мес. (как часто планируют вручную)
и «тот же месяц год назад».
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .forecast import censored_months, fit_sku, mi, prepare_sales, seasonality


def run(sales_raw: pd.DataFrame, stock_raw: pd.DataFrame, receipts: pd.DataFrame, cutoff_now: int, cfg: dict, back: int = 12) -> dict:
    sales = prepare_sales(sales_raw)
    full = sales[sales["mi"] < cutoff_now]
    cut = cutoff_now - back
    train = full[full["mi"] < cut]
    stock = stock_raw.copy()
    stock["mi"] = stock["month"].map(mi)
    stock = stock[stock["mi"] < cut]
    season = seasonality(train, cut, cfg["forecast"])
    rc = receipts[receipts["receipt_date"] < pd.Timestamp(cut // 12, cut % 12 + 1, 1)]
    cens = censored_months(stock, train, rc)
    stock_all = stock_raw.assign(mi=stock_raw["month"].map(mi))
    cens_test = censored_months(stock_all, full, receipts)  # месяцы теста, урезанные дефицитом

    recs = []
    for sku, g in train.groupby("sku_id"):
        last12 = g[g["mi"] >= cut - 12]
        if last12["y"].sum() == 0:
            continue
        cat = g["category"].iat[0]
        idx = season.sku.get(sku, season.category[cat])
        fc = fit_sku(g, idx, cut, cens, sku, cfg["forecast"])
        actual = full[(full["sku_id"] == sku) & (full["mi"] >= cut)].set_index("mi")["y"]
        ly = g.set_index("mi")["y"]
        naive = last12["y"].mean()
        for m in range(cut, cutoff_now):
            recs.append({"sku": sku, "category": cat, "mi": m, "actual": float(actual.get(m, 0.0)),
                         "model": fc.month(m), "avg12": naive, "same_month_ly": float(ly.get(m - 12, 0.0)),
                         "stockout": (sku, m) in cens_test})
    df = pd.DataFrame(recs)

    def wape(g, col):
        return float((g[col] - g["actual"]).abs().sum() / max(g["actual"].sum(), 1e-9) * 100)

    def bias(g, col):
        return float((g[col].sum() - g["actual"].sum()) / max(g["actual"].sum(), 1e-9) * 100)

    def block(g):
        # точность на горизонте закупки: сумма за 3 мес. (квартал) на SKU
        g2 = g.assign(q=(g["mi"] - cut) // 3).groupby(["sku", "q"])[["actual", "model", "avg12", "same_month_ly"]].sum()
        return {
            "wape_model": round(wape(g, "model"), 1), "wape_avg12": round(wape(g, "avg12"), 1),
            "wape_ly": round(wape(g, "same_month_ly"), 1),
            "wape_q_model": round(wape(g2, "model"), 1), "wape_q_avg12": round(wape(g2, "avg12"), 1),
            "wape_q_ly": round(wape(g2, "same_month_ly"), 1),
            "bias_model": round(bias(g, "model"), 1), "skus": int(g["sku"].nunique()),
        }

    clean = df[~df["stockout"]]
    out = {"period": [f"{cut // 12}-{cut % 12 + 1:02d}", f"{(cutoff_now - 1) // 12}-{(cutoff_now - 1) % 12 + 1:02d}"],
           "total": block(clean), "total_incl_stockouts": block(df),
           "stockout_share": round(float(df["stockout"].mean() * 100), 1), "by_category": {}}
    for c, g in clean.groupby("category"):
        out["by_category"][c] = block(g)
    monthly = clean.groupby("mi")[["actual", "model", "avg12"]].sum()
    out["monthly"] = [{"month": f"{m // 12}-{m % 12 + 1:02d}", **{k: round(float(v)) for k, v in r.items()}}
                      for m, r in monthly.iterrows()]
    cat_monthly = {}
    for c, g in clean.groupby("category"):
        mm = g.groupby("mi")[["actual", "model", "avg12"]].sum()
        cat_monthly[c] = [{k: round(float(v)) for k, v in r.items()} for _, r in mm.iterrows()]
    out["cat_monthly"] = cat_monthly
    return out
