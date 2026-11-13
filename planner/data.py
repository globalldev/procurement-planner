"""Загрузка выгрузки ERP, приём новых данных из inbox, определение даты среза."""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

FILES = ["sku_dim", "sales_monthly", "stock_monthly", "purchase_orders", "receipts", "supplier_dim"]


@dataclass
class Dataset:
    sku: pd.DataFrame
    sales: pd.DataFrame
    stock: pd.DataFrame
    po: pd.DataFrame
    receipts: pd.DataFrame
    suppliers: pd.DataFrame
    asof: date
    source: str


def ingest_inbox(inbox: Path, data_dir: Path) -> str | None:
    """Если в inbox лежит zip новее текущих данных — распаковать его в data_dir."""
    if not inbox.exists():
        return None
    # самая свежая выгрузка — по дате в имени файла, при равенстве — по времени изменения
    zips = sorted(inbox.glob("*.zip"), key=lambda p: (_asof_from_name(p.name) or date.min, p.stat().st_mtime))
    if not zips:
        return None
    latest = zips[-1]
    marker = data_dir / ".source"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == latest.name:
        return None
    tmp = data_dir.parent / "_unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(latest) as z:
        z.extractall(tmp)
    found = {p.stem: p for p in tmp.rglob("*.csv")}
    missing = [f for f in FILES if f not in found]
    if missing:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"В {latest.name} нет файлов: {missing}")
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(found[name], data_dir / f"{name}.csv")
    readme = next(tmp.rglob("README.md"), None)
    if readme:
        shutil.copy2(readme, data_dir / "README.md")
    marker.write_text(latest.name, encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return latest.name


def _asof_from_name(name: str) -> date | None:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def load(data_dir: Path, asof_cfg: str = "auto") -> Dataset:
    t = {f: pd.read_csv(data_dir / f"{f}.csv") for f in FILES}
    source = (data_dir / ".source").read_text(encoding="utf-8").strip() if (data_dir / ".source").exists() else data_dir.name

    if asof_cfg and asof_cfg != "auto":
        asof = datetime.strptime(asof_cfg, "%Y-%m-%d").date()
    else:
        asof = _asof_from_name(source)
        if asof is None:
            # нет даты в имени: последний известный день в данных
            last_rcpt = pd.to_datetime(t["receipts"]["receipt_date"]).max().date()
            last_month = t["sales_monthly"]["month"].max()
            asof = max(last_rcpt, datetime.strptime(last_month + "-01", "%Y-%m-%d").date())

    po = t["purchase_orders"].copy()
    for c in ("order_date", "promised_date"):
        po[c] = pd.to_datetime(po[c])
    rc = t["receipts"].copy()
    rc["receipt_date"] = pd.to_datetime(rc["receipt_date"])

    return Dataset(
        sku=t["sku_dim"], sales=t["sales_monthly"], stock=t["stock_monthly"],
        po=po, receipts=rc, suppliers=t["supplier_dim"], asof=asof, source=source,
    )
