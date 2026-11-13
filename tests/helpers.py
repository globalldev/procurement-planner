"""Маленькая синтетическая выгрузка для тестов: боевые данные в репозиторий не входят."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pandas as pd

from planner.cli import load_cfg
from planner.data import Dataset

ROOT = Path(__file__).resolve().parent.parent
ASOF = date(2026, 8, 13)


def cfg(**overrides) -> dict:
    """Боевой config.toml с подменой значений: cfg(policy__strict_pack=False)."""
    c = copy.deepcopy(load_cfg(ROOT / "config.toml"))
    for key, value in overrides.items():
        section, name = key.split("__")
        c[section][name] = value
    return c


def months(first: str = "2023-01", last: str = "2026-08") -> list[str]:
    out, (y, m) = [], map(int, first.split("-"))
    while f"{y}-{m:02d}" <= last:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def tables(skus: list[dict], last: str = "2026-08") -> dict[str, pd.DataFrame]:
    """skus: {id, category, sales: шт/мес или функция(месяц), stock: остаток на 1-е, markup, decile, orders, open_po}."""
    sku_rows, sales, stock, po, rc = [], [], [], [], []
    for n, s in enumerate(skus):
        sid, cat = s["id"], s.get("category", "cat_a")
        demand = s.get("sales", 10)
        sku_rows.append({"sku_id": sid, "category": cat, "brand_id": "BRD-1", "supplier_id": s.get("supplier", "SUP-1"),
                         "supply_type": "import", "weight_kg": None, "price_decile": s.get("decile", 5),
                         "avg_markup_pct_12m": s.get("markup", 40.0)})
        for mth in months(last=last):
            qty = demand(mth) if callable(demand) else demand
            if mth == last:
                qty = s.get("partial_sales", 0)
            sales.append({"sku_id": sid, "category": cat, "month": mth, "qty_sold": qty, "qty_returned": 0,
                          "partial_month": int(mth == last)})
        for mth in months("2024-01", last):
            st = s.get("stock", 100000)
            stock.append({"sku_id": sid, "month": mth, "stock_qty": st(mth) if callable(st) else st})
        for i, q in enumerate(s.get("orders", [100, 200, 300])):
            order, promised = pd.Timestamp(2025, 1 + i, 10), pd.Timestamp(2025, 1 + i, 10) + pd.Timedelta(days=120)
            po.append({"po_id": f"PO-{n}{i}", "sku_id": sid, "supplier_id": "SUP-1", "order_date": order,
                       "promised_date": promised, "planned_lead_days": 120, "status": "completed", "qty_ordered": q,
                       "qty_received": q, "qty_pending": 0})
            rc.append({"sku_id": sid, "receipt_date": promised, "qty": q, "supplier_id": "SUP-1"})
        for i, (promised, q) in enumerate(s.get("open_po", [])):
            po.append({"po_id": f"PO-OPEN-{n}{i}", "sku_id": sid, "supplier_id": "SUP-1", "order_date": pd.Timestamp(2026, 5, 1),
                       "promised_date": pd.Timestamp(promised), "planned_lead_days": 120, "status": "open", "qty_ordered": q,
                       "qty_received": 0, "qty_pending": q})
    return {
        "sku_dim": pd.DataFrame(sku_rows), "sales_monthly": pd.DataFrame(sales), "stock_monthly": pd.DataFrame(stock),
        "purchase_orders": pd.DataFrame(po), "receipts": pd.DataFrame(rc),
        "supplier_dim": pd.DataFrame([{"supplier_id": "SUP-1", "median_planned_lead_days": 120}]),
    }


def dataset(skus: list[dict], asof: date = ASOF, age_days: int = 0) -> Dataset:
    t = tables(skus)
    return Dataset(sku=t["sku_dim"], sales=t["sales_monthly"], stock=t["stock_monthly"], po=t["purchase_orders"],
                   receipts=t["receipts"], suppliers=t["supplier_dim"], asof=asof, source="test", age_days=age_days)


def write_csv(t: dict[str, pd.DataFrame], folder: Path, source: str = "procurement_dataset_20260813.zip") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, df in t.items():
        df.to_csv(folder / f"{name}.csv", index=False)
    (folder / ".source").write_text(source, encoding="utf-8")
    return folder
