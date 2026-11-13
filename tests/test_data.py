"""Выгрузка, по которой нельзя считать, должна останавливать расчёт, а не давать неверный план."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from planner import data
from tests.helpers import tables, write_csv

SKUS = [{"id": "SKU-A", "sales": 10}]


class LoadGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "raw"

    def tearDown(self):
        self.tmp.cleanup()

    def test_asof_from_zip_name_and_age(self):
        write_csv(tables(SKUS), self.dir)
        ds = data.load(self.dir, "auto", today=date(2026, 8, 20))
        self.assertEqual(ds.asof, date(2026, 8, 13))
        self.assertEqual(ds.age_days, 7)

    def test_asof_in_other_month_is_rejected(self):
        # срез «13 ноября» на августовских данных раньше молча давал план в 2,8 раза больше
        write_csv(tables(SKUS), self.dir)
        with self.assertRaises(data.DataError) as e:
            data.load(self.dir, "2026-11-13")
        self.assertIn("2026-08", str(e.exception))

    def test_zip_name_with_wrong_date_is_rejected(self):
        write_csv(tables(SKUS), self.dir, source="procurement_dataset_20261113.zip")
        with self.assertRaises(data.DataError):
            data.load(self.dir, "auto")

    def test_partial_flag_not_on_last_month_is_rejected(self):
        t = tables(SKUS)
        t["sales_monthly"].loc[t["sales_monthly"]["month"] == "2026-03", "partial_month"] = 1
        write_csv(t, self.dir)
        with self.assertRaises(data.DataError):
            data.load(self.dir, "auto")

    def test_no_partial_flag_allows_last_or_next_month(self):
        t = tables(SKUS)
        t["sales_monthly"]["partial_month"] = 0
        write_csv(t, self.dir)
        self.assertEqual(data.load(self.dir, "2026-09-01").asof, date(2026, 9, 1))
        with self.assertRaises(data.DataError):
            data.load(self.dir, "2026-10-01")

    def test_missing_column_is_named(self):
        t = tables(SKUS)
        t["stock_monthly"] = t["stock_monthly"].drop(columns=["stock_qty"])
        write_csv(t, self.dir)
        with self.assertRaises(data.DataError) as e:
            data.load(self.dir, "auto")
        self.assertIn("stock_qty", str(e.exception))


if __name__ == "__main__":
    unittest.main()
