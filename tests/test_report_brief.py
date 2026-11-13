"""«Что изменилось» сравнивает выгрузки, а не запуски; сводка ИИ не проходит с числами не из расчёта."""
import json
import tempfile
import unittest
from pathlib import Path

from planner import report
from planner.brief import _check_numbers


def snap(asof: str, generated: str, status: str = "ok") -> dict:
    return {"asof": asof, "generated_at": generated, "kpi": {"order_now_skus": 1},
            "skus": {"SKU-A": {"status": status, "qty": 0, "next": None, "deficit": None, "stock": 5}}}


class Baseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name: str, s: dict):
        (self.hist / name).write_text(json.dumps(s), encoding="utf-8")

    def test_rerun_on_same_data_does_not_become_the_baseline(self):
        self.put("20260806-070000.json", snap("2026-08-06", "2026-08-06T07:00:00"))
        self.put("20260813-070000.json", snap("2026-08-13", "2026-08-13T07:00:00"))
        self.put("2026-08-13.json", snap("2026-08-13", "2026-08-13T09:30:00"))     # перезапуск того же среза
        self.assertEqual(report.baseline(self.hist, "2026-08-13")["asof"], "2026-08-06")

    def test_latest_run_of_previous_dump_wins(self):
        self.put("a.json", snap("2026-08-06", "2026-08-06T07:00:00", status="ok"))
        self.put("b.json", snap("2026-08-06", "2026-08-06T12:00:00", status="order_now"))
        self.assertEqual(report.baseline(self.hist, "2026-08-13")["skus"]["SKU-A"]["status"], "order_now")

    def test_first_dump_has_nothing_to_compare_with(self):
        self.put("2026-08-13.json", snap("2026-08-13", "2026-08-13T07:00:00"))
        self.assertIsNone(report.baseline(self.hist, "2026-08-13"))
        self.assertEqual(report.diff(None, snap("2026-08-13", "x")), {"first_run": True})

    def test_diff_sees_new_order_and_new_sku(self):
        prev = snap("2026-08-06", "a")
        cur = snap("2026-08-13", "b", status="order_now")
        cur["skus"]["SKU-NEW"] = {"status": "order_now", "qty": 30, "next": None, "deficit": "critical", "stock": 0}
        d = report.diff(prev, cur)
        self.assertEqual([x["sku"] for x in d["new_order_now"]], ["SKU-A", "SKU-NEW"])
        self.assertEqual([x["sku"] for x in d["new_deficit"]], ["SKU-NEW"])
        self.assertEqual(d["prev_asof"], "2026-08-06")


CTX = json.dumps({"kpi": {"order_now_units": 49383, "order_now_skus": 147, "overdue": 339},
                  "backtest": {"wape_q_model": 54.0, "wape_q_avg12": 91.2}, "params": {"service_level_A": 0.95, "min_markup_pct": 25},
                  "top": [{"sku": "SKU-ABC123", "sup": "SUP-0851EB", "cover_until": "2026-08-26", "qty": 1000.0}]})


class NumberCheck(unittest.TestCase):
    def test_numbers_from_the_plan_pass(self):
        text = ("Разместить **147 SKU / 49 383 шт**, из них SKU-ABC123 (**1 000 шт**) у SUP-0851EB, дефицит с 26.08.2026. "
                "WAPE 54% против 91,2%, сервис 95%, наценка ниже 25%, просрочка 339 дней. 3 позиции на 12 месяцев.")
        self.assertEqual(_check_numbers(text, CTX), [])

    def test_made_up_values_are_caught(self):
        cases = {"Заказать 999 шт": "999", "Всего 12 345 шт": "12 345", "WAPE 71,5%": "71,5%", "Рост на 31%": "31%",
                 "Позиция SKU-FFFFFF": "SKU-FFFFFF", "Дефицит с 27.08.2026": "27.08.2026", "Поставщик SUP-000000": "SUP-000000"}
        for text, bad in cases.items():
            self.assertEqual(_check_numbers(text, CTX), [bad], text)

    def test_two_json_objects_in_context(self):
        self.assertEqual(_check_numbers("Потеря 25 878 шт", CTX + "\n" + json.dumps({"deficit_lost_units": 25878})), [])


if __name__ == "__main__":
    unittest.main()
