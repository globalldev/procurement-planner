"""Прогноз спроса: уровень × сезонный индекс.

Идея: продажи SKU в месяц m ≈ level × idx[m], где idx — сезонная форма
(категории, уточнённая собственной историей SKU), а level — текущий
«средний месяц» товара. Уровень оцениваем по последним месяцам с весами
(свежее важнее), исключая месяцы дефицита: ноль продаж при нулевом остатке —
это упущенный спрос, а не отсутствие спроса.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def mi(month: str) -> int:
    """'YYYY-MM' -> порядковый номер месяца."""
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def mi_str(i: int) -> str:
    return f"{i // 12}-{i % 12 + 1:02d}"


def _year_shapes(df: pd.DataFrame, cutoff: int, min_total: float) -> list[np.ndarray]:
    """Формы полных календарных лет до cutoff: 12 долей, нормированных к среднему 1."""
    shapes = []
    for year, g in df.groupby(df["mi"] // 12):
        if year * 12 + 11 >= cutoff or len(g) < 12:
            continue
        v = g.sort_values("mi")["y"].to_numpy(dtype=float)
        if v.sum() < min_total:
            continue
        shapes.append(v / v.mean())
    return shapes


@dataclass
class Seasonality:
    category: dict[str, np.ndarray]
    sku: dict[str, np.ndarray]
    sku_years: dict[str, int]


def seasonality(sales: pd.DataFrame, cutoff: int, cfg: dict) -> Seasonality:
    cat_idx: dict[str, np.ndarray] = {}
    cat = sales.groupby(["category", "mi"], as_index=False)["y"].sum()
    for c, g in cat.groupby("category"):
        shapes = _year_shapes(g, cutoff, min_total=12)
        idx = np.mean(shapes, axis=0) if shapes else np.ones(12)
        cat_idx[c] = idx / idx.mean()

    shrink = cfg["seasonal_shrink_years"]
    min_years = cfg["min_sku_years_for_own_season"]
    sku_idx, sku_years = {}, {}
    for sku, g in sales.groupby("sku_id"):
        c = g["category"].iat[0]
        shapes = _year_shapes(g, cutoff, min_total=24)
        n = len(shapes)
        sku_years[sku] = n
        if n >= min_years:
            own = np.mean(shapes, axis=0)
            blend = (n * own + shrink * cat_idx[c]) / (n + shrink)
            sku_idx[sku] = blend / blend.mean()
        else:
            sku_idx[sku] = cat_idx[c]
    return Seasonality(cat_idx, sku_idx, sku_years)


def censored_months(stock: pd.DataFrame, sales: pd.DataFrame, receipts: pd.DataFrame | None = None) -> set[tuple[str, int]]:
    """Месяцы, когда товар закончился: продажи в них — нижняя граница спроса, а не спрос.

    Доступно за месяц = остаток на 1-е (нет снимка = 0) + приходы за месяц.
    Если к концу месяца почти ничего не осталось — месяц «урезан дефицитом».
    Снимки остатков есть только с первого месяца в stock_monthly; раньше не судим.
    """
    if stock.empty:
        return set()
    first_snap = stock["mi"].min()
    st = dict(zip(zip(stock["sku_id"], stock["mi"]), stock["stock_qty"]))
    rc = {}
    if receipts is not None and len(receipts):
        r = receipts.assign(mi=receipts["receipt_date"].dt.year * 12 + receipts["receipt_date"].dt.month - 1)
        rc = r.groupby(["sku_id", "mi"])["qty"].sum().to_dict()
    out = set()
    sub = sales[sales["mi"] >= first_snap]
    for sku, m, y in zip(sub["sku_id"], sub["mi"], sub["qty_sold"]):
        avail = st.get((sku, m), 0) + rc.get((sku, m), 0)
        if avail - y <= max(1.0, 0.05 * avail):
            out.add((sku, m))
    return out


@dataclass
class SkuForecast:
    level: float           # спрос «среднего» месяца, шт
    sigma: float           # стандартное отклонение месячного спроса, шт
    idx: np.ndarray        # сезонный индекс (12)
    months_used: int
    months_censored: int
    history_months: int

    def month(self, m: int) -> float:
        return self.level * self.idx[m % 12]


def fit_sku(g: pd.DataFrame, idx: np.ndarray, cutoff: int, censored: set, sku: str, cfg: dict) -> SkuForecast:
    """g — продажи SKU (полные месяцы < cutoff)."""
    g = g[g["mi"] < cutoff]
    hist_months = len(g)
    H, hl = cfg["history_months"], cfg["half_life_months"]
    win_all = g[g["mi"] >= cutoff - H]
    cmask = np.array([(sku, m) in censored for m in win_all["mi"]], dtype=bool)
    n_cens = int(cmask.sum())
    win = win_all[~cmask]
    if win.empty and len(win_all):
        win = win_all          # всё окно в дефиците: берём продажи как нижнюю оценку
    if win.empty:
        return SkuForecast(0.0, 0.0, idx, 0, n_cens, hist_months)
    # месяцы дефицита, где продали больше текущей оценки, тоже информативны — добавим их
    if n_cens and len(win) < len(win_all):
        cw = win_all[cmask]
        a0 = (cutoff - 1 - win["mi"]).to_numpy()
        w0 = 0.5 ** (a0 / hl)
        s0 = idx[win["mi"].to_numpy() % 12]
        lvl0 = (w0 * win["y"].to_numpy()).sum() / max((w0 * s0).sum(), 1e-9)
        extra = cw[cw["y"].to_numpy() > lvl0 * idx[cw["mi"].to_numpy() % 12]]
        if len(extra):
            win = pd.concat([win, extra]).sort_values("mi")
    age = (cutoff - 1 - win["mi"]).to_numpy()
    w = 0.5 ** (age / hl)
    y = win["y"].to_numpy(dtype=float)
    s = idx[win["mi"].to_numpy() % 12]
    floor = cfg.get("min_season_coverage", 0.5)

    def ratio(yv):
        # если в окне только «мёртвый сезон», не раздуваем уровень делением на крошечный индекс
        return float((w * yv).sum() / (w.sum() * max((w * s).sum() / w.sum(), floor)))

    level = ratio(y)
    # разовые оптовые отгрузки: срезаем месяцы выше k × ожидаемого и пересчитываем
    k = cfg.get("outlier_k", 3.0)
    if level > 0:
        y = np.minimum(y, k * level * np.maximum(s, 0.2))
        level = ratio(y)
    # тренд: свежие 6 мес. против предыдущих 6, с затуханием
    td = cfg.get("trend_damping", 0.0)
    if td and level > 0:
        rec, old = win["mi"].to_numpy() >= cutoff - 6, win["mi"].to_numpy() < cutoff - 6
        if rec.sum() >= 3 and old.sum() >= 3:
            a = y[rec].sum() / max(s[rec].sum(), floor * rec.sum())
            b = y[old].sum() / max(s[old].sum(), floor * old.sum())
            if b > 0:
                level *= float(np.clip(a / b, 0.5, 1.5)) ** td

    # разброс: остатки модели за 24 мес (без месяцев дефицита)
    w24 = g[g["mi"] >= cutoff - 24]
    w24 = w24[[(sku, m) not in censored for m in w24["mi"]]]
    # разброс считаем в «очищенном от сезона» масштабе: сезонность потом добавится один раз, в политике
    si = np.maximum(idx[w24["mi"].to_numpy() % 12], 0.3)
    resid = w24["y"].to_numpy() / si - level
    sigma = float(np.std(resid, ddof=1)) if len(resid) >= 4 else float(np.sqrt(max(level, 0)))
    return SkuForecast(level, sigma, idx, len(win), n_cens, hist_months)


def prepare_sales(sales: pd.DataFrame) -> pd.DataFrame:
    s = sales.copy()
    s["mi"] = s["month"].map(mi)
    s["y"] = (s["qty_sold"] - s["qty_returned"].fillna(0)).clip(lower=0)
    return s
