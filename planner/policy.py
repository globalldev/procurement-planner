"""Политика закупки: проекция остатков по дням и календарь заказов по каждому SKU.

Для каждого товара:
1. остаток сейчас = снимок на 1-е число − продажи с тех пор + приходы с тех пор;
2. в пути = открытые PO (qty_pending) с плановой датой прихода;
3. прогноз спроса по дням = level × сезонный индекс месяца / дней в месяце;
4. проекция остатка вперёд (продажи не уходят в минус: нет товара — потерянный спрос);
5. дата заказа = день, когда остаток упадёт ниже страхового запаса, минус плечо;
6. объём = спрос на [приход, приход + цикл] + страховой − ожидаемый остаток на приход − что ещё приедет
   в этот промежуток (включая день прихода); округление вверх до кратности партии из истории заказов.
Шаги 5–6 повторяются, пока на 12 месяцев вперёд не построен календарь заказов.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

from .forecast import SkuForecast, mi

PACKS = [1000, 500, 250, 200, 100, 50, 25, 20, 10, 5, 1]


@dataclass
class Calendar:
    asof: date
    horizon: int
    day_mi: np.ndarray = field(init=False)
    day_dim: np.ndarray = field(init=False)

    def __post_init__(self):
        days = [self.asof + timedelta(d) for d in range(self.horizon)]
        self.day_mi = np.array([d.year * 12 + d.month - 1 for d in days])
        self.day_dim = np.array([calendar.monthrange(d.year, d.month)[1] for d in days], dtype=float)

    def date(self, d: int) -> date:
        return self.asof + timedelta(int(d))

    def daily_demand(self, fc: SkuForecast) -> np.ndarray:
        return fc.level * fc.idx[self.day_mi % 12] / self.day_dim


def project(stock0: float, demand: np.ndarray, arrivals: np.ndarray):
    """Остаток на конец каждого дня и потерянный спрос (без бэкордеров)."""
    n = len(demand)
    inv = np.empty(n)
    lost = np.empty(n)
    cur = stock0
    for d in range(n):
        cur += arrivals[d]
        sold = demand[d] if demand[d] <= cur else cur
        lost[d] = demand[d] - sold
        cur -= sold
        inv[d] = cur
    return inv, lost


def first_below(inv: np.ndarray, level: float, start: int = 0) -> int | None:
    idx = np.nonzero(inv[start:] < level - 1e-9)[0]
    return int(idx[0]) + start if len(idx) else None


def first_below_arr(inv: np.ndarray, thr: np.ndarray, start: int = 0) -> int | None:
    idx = np.nonzero(inv[start:] < thr[start:] - 1e-9)[0]
    return int(idx[0]) + start if len(idx) else None


def effective_pack(pack: int, q: float) -> int:
    """Кратность из истории, но маленькую потребность не раздуваем до большой партии."""
    for p in PACKS:
        if pack % p == 0 and p <= max(q / 2, 1):
            return p
    return 1


def pack_size(hist_qty: list[float]) -> int:
    q = [int(x) for x in hist_qty if x and x > 0]
    if not q:
        return 1
    med = float(np.median(q))
    for p in PACKS:
        if p <= max(med / 4, 1) and all(x % p == 0 for x in q):
            return p
    return 1


def lead_days(sku: str, supplier: str, supply_type: str, po: pd.DataFrame, sup: dict, cfg: dict) -> tuple[int, str]:
    """Плечо: заказ поставщику едет одной партией, поэтому сначала медиана по поставщику (PO за 2 года),
    затем история самого SKU, затем справочник поставщиков, затем норматив по типу поставки."""
    lt = cfg["lead_time"]
    ok = po["planned_lead_days"].between(7, 400)
    recent = po["order_date"] >= po["order_date"].max() - pd.Timedelta(days=730)
    by_sup = po[ok & recent & (po["supplier_id"] == supplier)]
    own = po[ok & (po["sku_id"] == sku)].sort_values("order_date")
    if len(by_sup) >= 3:
        base, src = float(by_sup["planned_lead_days"].median()), "медиана PO поставщика"
    elif len(own):
        base, src = float(own["planned_lead_days"].tail(5).median()), "история заказов SKU"
    elif supplier in sup and pd.notna(sup[supplier]):
        base, src = float(sup[supplier]), "справочник поставщика"
    else:
        base = {"import": lt["default_import_days"], "local": lt["default_local_days"]}.get(
            supply_type, lt["default_unknown_days"])
        src = "норматив по типу поставки"
    # запас на задержку поставки, но итог не выше верхней границы плеча (180 дн. по условию)
    total = min(max(base, lt["min_days"]) + lt["buffer_days"], lt["max_days"])
    return int(round(total)), src


def z_for(abc: str, cfg: dict) -> float:
    p = cfg["policy"].get(f"service_level_{abc}", 0.9)
    return NormalDist().inv_cdf(p)


def plan_sku(
    *, cal: Calendar, fc: SkuForecast, stock0: float, transit: list[dict], L: int, abc: str,
    pack: int, cfg: dict,
) -> dict:
    pol = cfg["policy"]
    H, R = cal.horizon, pol["review_days"]
    demand = cal.daily_demand(fc)
    arrivals = np.zeros(H)
    for t in transit:
        if t["counted"] and 0 <= t["day"] < H:
            arrivals[t["day"]] += t["qty"]
        elif t["counted"] and t["day"] < 0:
            arrivals[0] += t["qty"]

    ss = z_for(abc, cfg) * fc.sigma * np.sqrt(L / 30.0)
    ss = float(min(ss, pol["max_safety_months"] * fc.level)) if fc.level > 0 else 0.0
    # страховой запас дышит вместе с сезоном: в пик больше, в межсезонье меньше
    ss_day = np.minimum(ss * np.clip(fc.idx[cal.day_mi % 12], 0.1, 2.0), pol["max_safety_months"] * fc.level)

    inv_base, lost_base = project(stock0, demand, arrivals)
    inv_nt, _ = project(stock0, demand, np.zeros(H))

    def stockout_day(inv):
        d = np.nonzero((inv <= 1e-9) & (demand > 1e-9))[0]
        return int(d[0]) if len(d) else None

    so_with = stockout_day(inv_base)
    so_without = stockout_day(inv_nt)

    orders = []
    arr = arrivals.copy()
    inv = inv_base
    skip_to = 0
    season_w = fc.idx[cal.day_mi % 12]
    off_thr = pol.get("offseason_index", 0.5)
    if fc.level > 0:
        for _ in range(600):
            if len(orders) >= 8:
                break
            start = max((orders[-1]["arrival_day"] + 1) if orders else 0, skip_to)
            t_star = first_below_arr(inv, ss_day, start)
            if t_star is None:
                break
            order_day = max(t_star - L, 0)
            if order_day > 365:
                break
            if orders and order_day < orders[-1]["order_day"]:
                order_day = orders[-1]["order_day"]
            a = order_day + L
            if a >= H:
                break
            end = min(a + R, H)
            # приход в мёртвый сезон (обогреватели в феврале) не нужен: переносим заказ к следующему сезону
            if season_w[a:end].mean() < off_thr and season_w.max() > 1.5:
                skip_to = t_star + 1
                continue
            need = demand[a:end].sum() + ss_day[end - 1]
            on_arrival = inv[a - 1] if a > 0 else stock0
            later = arr[a:end].sum()   # всё, что ещё приедет с дня прихода включительно: inv[a-1] приход дня a не содержит
            q = need - on_arrival - later
            if q <= 0.5:
                # ниже страхового только кратковременно — сдвигаем точку поиска
                skip_to = t_star + 1
                continue
            raw = float(q)
            # кратность партии поставщика соблюдаем строго; strict_pack = false — малую потребность до партии не раздуваем
            strict = pol.get("strict_pack", True)
            step = pack if strict else effective_pack(pack, q)
            q = int(np.ceil(raw / step) * step)
            # партия больше спроса за горизонт избытка (год): округление само создало бы избыток на годы.
            # Такой заказ не раздуваем — показываем потребность и отдаём решение закупщику (below_pack).
            below = bool(strict and pack > 1 and q > fc.level * pol["excess_months"])
            if below:
                step = effective_pack(pack, raw)
                q = int(np.ceil(raw / step) * step)
            gap = pol.get("min_order_gap_days", 30)
            if orders and order_day - orders[-1]["order_day"] < gap:
                # слишком частые заказы: докладываем в предыдущий
                orders[-1]["qty"] += q
                orders[-1]["need"] += raw
                orders[-1]["below_pack"] = orders[-1]["below_pack"] or below
                arr[orders[-1]["arrival_day"]] += q
            else:
                orders.append({"order_day": int(order_day), "arrival_day": int(a), "qty": q, "need": raw, "below_pack": below})
                arr[a] += q
            inv, _ = project(stock0, demand, arr)

    inv_plan, lost_plan = project(stock0, demand, arr)

    # дефицит до прихода нового заказа: рекомендованного сейчас, иначе — самого раннего возможного
    end = orders[0]["arrival_day"] if orders and orders[0]["order_day"] <= pol["order_now_window_days"] else L
    end = min(end, H)
    lost_before_L = float(lost_base[:end].sum())
    deficit = None
    if so_with is not None and so_with < end and lost_before_L >= pol.get("min_deficit_units", 3):
        deficit = {
            "stockout_day": so_with,
            "gap_days": int(end - so_with),
            "lost_units": round(lost_before_L),
            "severity": "critical" if so_with <= 30 else "high",
        }

    exc_days = pol["excess_months"] * 30
    on_hand_all = stock0 + sum(t["qty"] for t in transit if t["counted"])
    need_exc = float(demand[:exc_days].sum())
    excess = None
    if on_hand_all > need_exc + ss and on_hand_all > 0:
        excess = {"excess_units": round(on_hand_all - need_exc - ss)}

    return {
        "demand": demand, "ss": ss, "orders": orders, "inv_base": inv_base, "inv_plan": inv_plan,
        "lost_plan": float(lost_plan[:365].sum()), "stockout_with": so_with, "stockout_without": so_without,
        "deficit": deficit, "excess": excess, "on_hand_all": on_hand_all,
    }


def stock_now(sku_sales: pd.DataFrame, sku_stock: pd.DataFrame, sku_rcpt: pd.DataFrame, snap_mi: int) -> float:
    snap = sku_stock.loc[sku_stock["mi"] == snap_mi, "stock_qty"]
    base = float(snap.iat[0]) if len(snap) else 0.0
    sold = float(sku_sales.loc[sku_sales["mi"] >= snap_mi, "y"].sum())
    y, m = divmod(snap_mi, 12)
    got = float(sku_rcpt.loc[sku_rcpt["receipt_date"] >= pd.Timestamp(y, m + 1, 1), "qty"].sum())  # приходы с 1-го числа
    return max(base - sold + got, 0.0)


def transit_for(sku_po: pd.DataFrame, asof: date, cfg: dict) -> list[dict]:
    pol = cfg["policy"]
    out = []
    for _, r in sku_po[(sku_po["status"] == "open") & (sku_po["qty_pending"] > 0)].iterrows():
        prom = r["promised_date"].date()
        day = (prom - asof).days
        overdue = -day if day < 0 else 0
        counted = overdue <= pol["overdue_trust_days"]
        eff = day if day >= 0 else pol["overdue_grace_days"]
        out.append({
            "po_id": r["po_id"], "qty": float(r["qty_pending"]), "promised": prom.isoformat(),
            "day": int(eff), "overdue_days": int(overdue), "counted": bool(counted),
        })
    return out


__all__ = ["Calendar", "plan_sku", "stock_now", "transit_for", "lead_days", "pack_size", "mi"]
