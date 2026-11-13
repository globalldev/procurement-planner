"""Загрузка выгрузки ERP, приём новых данных из inbox, определение даты среза."""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

FILES = ["sku_dim", "sales_monthly", "stock_monthly", "purchase_orders", "receipts", "supplier_dim"]

# колонки, без которых расчёт невозможен (остальные поля выгрузки необязательны)
REQUIRED = {
    "sku_dim": ["sku_id", "category", "brand_id", "supplier_id", "supply_type", "weight_kg", "price_decile",
                "avg_markup_pct_12m"],
    "sales_monthly": ["sku_id", "category", "month", "qty_sold", "qty_returned"],
    "stock_monthly": ["sku_id", "month", "stock_qty"],
    "purchase_orders": ["po_id", "sku_id", "supplier_id", "order_date", "promised_date", "planned_lead_days", "status",
                        "qty_ordered", "qty_received", "qty_pending"],
    "receipts": ["sku_id", "receipt_date", "qty", "supplier_id"],
    "supplier_dim": ["supplier_id", "median_planned_lead_days"],
}


class DataError(ValueError):
    """Выгрузка не годится для расчёта: считать по ней — значит выдать неверный план."""


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
    age_days: int = 0                                  # сколько дней выгрузке на момент расчёта
    warnings: list[str] = field(default_factory=list)  # что закупщик должен знать о данных


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


def _next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def check_asof(asof: date, sales: pd.DataFrame) -> None:
    """Дата среза должна попадать в неполный месяц выгрузки.

    Текущий месяц расчёт берёт из даты среза: если она не про эти данные, неполный месяц пойдёт в оценку спроса
    как полный, а остаток «на сегодня» окажется остатком прошлых месяцев — план выйдет неверным без единой ошибки.
    """
    am = f"{asof:%Y-%m}"
    last = str(sales["month"].max())
    partial = sorted(sales.loc[sales["partial_month"] == 1, "month"].unique()) if "partial_month" in sales else []
    if partial:
        if partial != [last]:
            raise DataError(f"В sales_monthly неполным помечен не последний месяц ({', '.join(partial)}; последний {last}) — "
                            "выгрузка повреждена.")
        if am != last:
            raise DataError(f"Дата среза {asof} не совпадает с неполным месяцем выгрузки ({last}). "
                            "Проверьте дату в имени zip или параметр --asof.")
    elif am not in (last, _next_month(last)):
        raise DataError(f"Дата среза {asof} не согласуется с выгрузкой: последний месяц продаж {last}. "
                        "Проверьте дату в имени zip или параметр --asof.")


def load(data_dir: Path, asof_cfg: str = "auto", today: date | None = None) -> Dataset:
    t = {}
    for f in FILES:
        path = data_dir / f"{f}.csv"
        if not path.exists():
            raise DataError(f"Нет файла {path} — положите zip с выгрузкой в data/inbox/.")
        t[f] = pd.read_csv(path)
        missing = [c for c in REQUIRED[f] if c not in t[f].columns]
        if missing:
            raise DataError(f"В {f}.csv нет колонок: {', '.join(missing)}.")
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
    check_asof(asof, t["sales_monthly"])
    age = ((today or date.today()) - asof).days

    po = t["purchase_orders"].copy()
    for c in ("order_date", "promised_date"):
        po[c] = pd.to_datetime(po[c])
    rc = t["receipts"].copy()
    rc["receipt_date"] = pd.to_datetime(rc["receipt_date"])

    return Dataset(
        sku=t["sku_dim"], sales=t["sales_monthly"], stock=t["stock_monthly"],
        po=po, receipts=rc, suppliers=t["supplier_dim"], asof=asof, source=source, age_days=age,
    )
