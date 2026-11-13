"""Дефицитные месяцы и ровные категории в прогнозе; публикация коммитит только docs/."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planner import publish
from planner.forecast import censored_months, mi, prepare_sales, seasonality
from tests.helpers import cfg, tables


class CensoredMonths(unittest.TestCase):
    def frames(self, stock_qty, sold):
        m = mi("2026-05")
        stock = pd.DataFrame([{"sku_id": "SKU-A", "mi": m, "stock_qty": stock_qty}])
        sales = pd.DataFrame([{"sku_id": "SKU-A", "mi": m, "qty_sold": sold}])
        return stock, sales, m

    def test_sold_out_month_is_censored(self):
        stock, sales, m = self.frames(100, 97)
        self.assertEqual(censored_months(stock, sales), {("SKU-A", m)})

    def test_leftover_stock_is_censored_only_for_evaluation(self):
        # 5 шт на складе при обычном спросе 400: продали 1 — это не «спроса нет»
        stock, sales, m = self.frames(5, 1)
        self.assertEqual(censored_months(stock, sales), set())
        self.assertEqual(censored_months(stock, sales, usual={("SKU-A", m): 400.0}, min_share=0.5), {("SKU-A", m)})
        self.assertEqual(censored_months(stock, sales, usual={("SKU-A", m): 8.0}, min_share=0.5), set())


class FlatSeasonality(unittest.TestCase):
    def test_flat_category_gets_no_seasonality(self):
        wobble = {"01": 12, "02": 8, "03": 11, "04": 9, "05": 13, "06": 10, "07": 12, "08": 8, "09": 10, "10": 9, "11": 11, "12": 7}
        peak = lambda m: 500 if m[5:] == "05" else 10
        t = tables([{"id": "SKU-FLAT", "category": "flat", "sales": lambda m: wobble[m[5:]] * 10},
                    {"id": "SKU-PEAK", "category": "seasonal", "sales": peak}])
        sales = prepare_sales(t["sales_monthly"])
        s = seasonality(sales[sales["mi"] < mi("2026-08")], mi("2026-08"), cfg()["forecast"])
        self.assertEqual(s.category["flat"].tolist(), [1.0] * 12)
        self.assertEqual(s.sku["SKU-FLAT"].tolist(), [1.0] * 12)
        self.assertGreater(s.category["seasonal"][4], 5)      # май остаётся пиком


@unittest.skipUnless(shutil.which("git"), "нужен git")
class PublishCommitsOnlyDocs(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        for k, v in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        (self.root / "docs").mkdir()
        (self.root / "out").mkdir()
        (self.root / "docs" / "index.html").write_text("v1", encoding="utf-8")
        (self.root / "notes.txt").write_text("v1", encoding="utf-8")
        (self.root / "out" / "plan.json").write_text(json.dumps({"meta": {"asof": "2026-08-13"}}), encoding="utf-8")
        self.git("add", "docs", "notes.txt")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_staged_work_stays_out_of_the_publish_commit(self):
        (self.root / "docs" / "index.html").write_text("v2", encoding="utf-8")
        (self.root / "notes.txt").write_text("черновик, не для публикации", encoding="utf-8")
        self.git("add", "notes.txt")
        ok, msg = publish.publish(self.root, {"publish": {"method": "git", "url": "https://example.invalid/"}})
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["docs/index.html"])
        self.assertEqual(self.git("diff", "--cached", "--name-only").split(), ["notes.txt"])
        # у временного репозитория нет remote: push не удался, и это видно по результату
        self.assertFalse(ok)
        self.assertIn("git push", msg)

    def test_nothing_to_publish_is_not_an_error_of_commit(self):
        ok, msg = publish.publish(self.root, {"publish": {"method": "git"}})
        self.assertIn("git push", msg)            # коммит пропущен, упали только на push без remote
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), "1")


if __name__ == "__main__":
    unittest.main()
