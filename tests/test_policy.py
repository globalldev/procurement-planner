"""Объём заказа: кратность партии и учёт того, что уже едет."""
import unittest

import numpy as np

from planner.forecast import SkuForecast
from planner.policy import Calendar, plan_sku
from tests.helpers import ASOF, cfg

L = 100


def plan(pack=1, transit=(), level=30.0, **overrides):
    c = cfg(**overrides)
    cal = Calendar(ASOF, c["policy"]["horizon_days"])
    fc = SkuForecast(level=level, sigma=5.0, idx=np.ones(12), months_used=12, months_censored=0, history_months=36)
    return plan_sku(cal=cal, fc=fc, stock0=0.0, transit=list(transit), L=L, abc="B", pack=pack, cfg=c)


class PackMultiple(unittest.TestCase):
    def test_every_order_is_a_multiple_of_pack(self):
        orders = plan(pack=50)["orders"]
        self.assertTrue(orders)
        self.assertEqual([o["qty"] % 50 for o in orders], [0] * len(orders))
        self.assertFalse(any(o["below_pack"] for o in orders))

    def test_need_below_pack_is_rounded_up_to_a_full_pack(self):
        first = plan(pack=50, level=5.0)["orders"][0]     # спрос 60 шт в год, потребность меньше партии
        self.assertLess(first["need"], 40)
        self.assertEqual(first["qty"], 50)
        self.assertFalse(first["below_pack"])

    def test_pack_bigger_than_yearly_demand_is_not_forced(self):
        # спрос 6 шт в год, партия 50: округление дало бы запас на 8 лет — оставляем потребность и помечаем позицию
        first = plan(pack=50, level=0.5)["orders"][0]
        self.assertTrue(first["below_pack"])
        self.assertLess(first["qty"], 50)

    def test_flexible_mode_does_not_inflate_small_need(self):
        first = plan(pack=50, level=5.0, policy__strict_pack=False)["orders"][0]
        self.assertLess(first["qty"], 50)
        self.assertFalse(first["below_pack"])


class TransitOnArrivalDay(unittest.TestCase):
    def test_po_arriving_on_the_same_day_reduces_the_order(self):
        base = plan()["orders"][0]
        self.assertEqual((base["order_day"], base["arrival_day"]), (0, L))
        po = {"po_id": "PO-1", "qty": 40.0, "promised": "2026-11-21", "day": L, "overdue_days": 0, "counted": True}
        with_po = plan(transit=[po])["orders"][0]
        self.assertEqual(with_po["arrival_day"], L)
        # раньше приход в день прихода нового заказа не попадал ни в остаток, ни в «ещё приедет» — заказ был на 40 шт больше
        self.assertAlmostEqual(base["need"] - with_po["need"], 40.0, places=6)


if __name__ == "__main__":
    unittest.main()
