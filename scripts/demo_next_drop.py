"""Демо регулярного запуска: собрать «выгрузку через неделю» из текущей.

Что меняется (как было бы в реальной ERP через 7 дней):
- заказы с плановым приходом в эти дни приходят на склад (receipts + статус completed);
- продажи неполного месяца растут пропорционально прошедшим дням;
- в имени zip — новая дата среза.

Запуск:  python scripts/demo_next_drop.py 2026-08-20
Затем:   python -m planner all   (заберёт zip из data/inbox и покажет «что изменилось»)
"""
from __future__ import annotations

import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def main(new_asof: str) -> None:
    raw = ROOT / "data" / "raw"
    src_name = (raw / ".source").read_text(encoding="utf-8").strip()
    old = datetime.strptime(src_name.rsplit("_", 1)[-1][:8], "%Y%m%d").date()
    new = datetime.strptime(new_asof, "%Y-%m-%d").date()
    assert new > old and new.month == old.month, "демо: дата в пределах того же месяца"

    t = {f.stem: pd.read_csv(f) for f in raw.glob("*.csv")}
    po, rc, sales = t["purchase_orders"], t["receipts"], t["sales_monthly"]

    arrive = (po["status"] == "open") & (po["qty_pending"] > 0) & \
             (pd.to_datetime(po["promised_date"]).dt.date > old) & (pd.to_datetime(po["promised_date"]).dt.date <= new)
    new_rc = po.loc[arrive, ["sku_id", "promised_date", "qty_pending", "supplier_id", "supply_type"]] \
        .rename(columns={"promised_date": "receipt_date", "qty_pending": "qty"})
    po.loc[arrive, "qty_received"] += po.loc[arrive, "qty_pending"]
    po.loc[arrive, ["qty_pending"]] = 0
    po.loc[arrive, "status"] = "completed"
    t["receipts"] = pd.concat([rc, new_rc], ignore_index=True)
    t["purchase_orders"] = po

    k = new.day / old.day
    part = sales["partial_month"] == 1
    for c in ("qty_sold", "orders_cnt"):
        sales.loc[part, c] = (sales.loc[part, c] * k).round().astype(int)
    t["sales_monthly"] = sales

    inbox = ROOT / "data" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    out = inbox / f"procurement_dataset_{new:%Y%m%d}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, df in t.items():
            z.writestr(f"share/{name}.csv", df.to_csv(index=False))
        if (raw / "README.md").exists():
            z.write(raw / "README.md", "share/README.md")
    print(f"{out.name}: пришло {int(arrive.sum())} строк PO ({int(new_rc['qty'].sum())} шт), продажи месяца ×{k:.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat())
