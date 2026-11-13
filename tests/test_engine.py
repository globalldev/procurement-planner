"""Правила сборки плана: мёртвый ассортимент, ABC, дефицит, свежесть выгрузки."""
import unittest

from planner import engine
from tests.helpers import cfg, dataset


def by_sku(result: dict) -> dict:
    return {r["sku"]: r for r in result["skus"]}


class DeadAssortment(unittest.TestCase):
    # продажи шли до ноября 2025 включительно, дальше 8 полных месяцев тишины
    SKUS = [{"id": "SKU-LIVE", "sales": 50},
            {"id": "SKU-OLD", "sales": lambda m: 20 if m <= "2025-11" else 0}]

    def test_dead_months_from_config_is_used(self):
        ds = dataset(self.SKUS)
        self.assertEqual(by_sku(engine.run(ds, cfg(economics__dead_months=12)))["SKU-OLD"]["status"] == "dead", False)
        self.assertEqual(by_sku(engine.run(ds, cfg(economics__dead_months=6)))["SKU-OLD"]["status"], "dead")


class AbcInsideCategory(unittest.TestCase):
    def test_leader_of_each_category_is_a(self):
        # у категории cat_b оборот в сотни раз меньше: при общем на весь ассортимент ABC её лидер был бы классом C
        skus = [{"id": "SKU-A1", "category": "cat_a", "sales": 1000}, {"id": "SKU-A2", "category": "cat_a", "sales": 100},
                {"id": "SKU-A3", "category": "cat_a", "sales": 10},
                {"id": "SKU-B1", "category": "cat_b", "sales": 6}, {"id": "SKU-B2", "category": "cat_b", "sales": 1}]
        r = by_sku(engine.run(dataset(skus), cfg()))
        self.assertEqual(r["SKU-A1"]["abc"], "A")
        self.assertEqual(r["SKU-B1"]["abc"], "A")
        self.assertEqual(r["SKU-A3"]["abc"], "C")


class DeficitAndLowMarkup(unittest.TestCase):
    SKUS = [{"id": "SKU-OK", "sales": 100, "stock": lambda m: 0 if m == "2026-08" else 100000},
            {"id": "SKU-LOW", "sales": 100, "stock": lambda m: 0 if m == "2026-08" else 100000, "markup": 10.0}]

    def test_low_markup_is_not_a_deficit_risk(self):
        res = engine.run(dataset(self.SKUS), cfg())
        r = by_sku(res)
        self.assertEqual(r["SKU-LOW"]["status"], "low_markup")
        self.assertIsNone(r["SKU-LOW"]["deficit"])
        self.assertIsNotNone(r["SKU-OK"]["deficit"])
        self.assertEqual(res["kpi"]["deficit_skus"], 1)
        self.assertEqual(res["kpi"]["deficit_out_now"], 1)
        self.assertEqual(res["kpi"]["deficit_lost_units"], r["SKU-OK"]["deficit"]["lost_units"])


class StaleData(unittest.TestCase):
    SKUS = [{"id": "SKU-A", "sales": 10}]

    def test_old_dump_is_flagged(self):
        res = engine.run(dataset(self.SKUS, age_days=92), cfg(data__stale_days=7))
        self.assertTrue(res["meta"]["stale"])
        self.assertIn("92", res["meta"]["warnings"][0])
        self.assertEqual(res["data_quality"][0]["title"], "Выгрузка устарела")

    def test_fresh_dump_and_disabled_check(self):
        self.assertFalse(engine.run(dataset(self.SKUS, age_days=3), cfg(data__stale_days=7))["meta"]["stale"])
        self.assertFalse(engine.run(dataset(self.SKUS, age_days=92), cfg(data__stale_days=0))["meta"]["stale"])


if __name__ == "__main__":
    unittest.main()
