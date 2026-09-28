import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ate import UserError, add, cell, parser, prepare, range_name


CONFIG = {"food_sheet": "資料庫", "log_sheet": "log", "timezone": "Asia/Taipei", "spreadsheet_id": "test"}
CATALOG = [["毛豆", 116, 14.6, 3.3, 12.5, 6.5, "豆"], ["土魠魚", 193, 19, 13, 0, "", "魚"]]


def args(*extra):
    return parser().parse_args(["add", "--date", "2026-09-28", "--request-id", "discord:123:1", *extra])


class Sheet:
    id = 10
    title = "log"

    def get(self, span, **kwargs):
        if span == "B2:H":
            return CATALOG
        if span == "A1:K1":
            return [["Date", "體重", "Fat", "Food", "g", "估計", "熱量", "蛋白質(g)", "脂肪(g)", "碳水化合物(g)", "膳食纖維(g)"]]
        return [["2026/09/28", "", "", "毛豆", 60, "", 69.6, 8.76, 1.98, 7.5, 3.9]]


class Book:
    def __init__(self):
        self.notes = []
        self.writes = []
        self.fail_after_commit = False

    def worksheet(self, name):
        return Sheet()

    def fetch_sheet_metadata(self, params):
        return {"sheets": [{"data": [{"startRow": 385, "rowData": [{"values": [{"note": n}]} for n in self.notes]}]}]}

    def batch_update(self, body):
        self.writes.append(body)
        self.notes.append(body["requests"][0]["appendCells"]["rows"][0]["values"][0]["note"])
        if self.fail_after_commit:
            raise TimeoutError("response lost after commit")


class CalculationTests(unittest.TestCase):
    def test_database_per_100g(self):
        cells, entry = prepare(args("--mode", "food", "--food", "毛豆", "--grams", "60"), CONFIG, CATALOG)
        self.assertEqual(entry["kcal"], 69.6)
        self.assertEqual(entry["protein"], 8.76)
        self.assertIn("formulaValue", cells[6]["userEnteredValue"])
        self.assertEqual(cells[7:], [{}, {}, {}, {}])
        self.assertEqual(cells[1:3], [{}, {}])
        self.assertEqual(cells[0]["userEnteredValue"]["numberValue"], 46293)

    def test_blank_fiber_not_zero_and_database_kcal_used(self):
        _, entry = prepare(args("--mode", "food", "--food", "土魠魚", "--grams", "100"), CONFIG, CATALOG)
        self.assertEqual(entry["kcal"], 193)
        self.assertIsNone(entry["fiber"])
        self.assertEqual(entry["carbs"], 0)

    def test_macro_estimate(self):
        _, entry = prepare(args("--mode", "macros", "--food", "自製餐", "--protein", "30", "--fat", "20", "--carbs", "60"), CONFIG, [])
        self.assertEqual(entry["kcal"], 540)
        self.assertIsNone(entry["grams"])

    def test_kcal_unknown_macros(self):
        cells, entry = prepare(args("--mode", "kcal", "--food", "便當", "--kcal", "700"), CONFIG, [])
        self.assertEqual(cells[5]["userEnteredValue"]["numberValue"], 700)
        self.assertEqual(cells[6]["userEnteredValue"]["numberValue"], 700)
        self.assertIsNone(entry["protein"])

    def test_reject_missing_macro(self):
        with self.assertRaises(UserError):
            prepare(args("--mode", "macros", "--food", "餐", "--protein", "30", "--fat", "20"), CONFIG, [])

    def test_zero_is_explicit(self):
        _, e = prepare(args("--mode", "macros", "--food", "餐", "--protein", "0", "--fat", "0", "--carbs", "0"), CONFIG, [])
        self.assertEqual(e["kcal"], 0)

    def test_invalid_inputs(self):
        for grams in ["-1", "0", "NaN", "Infinity", "abc"]:
            with self.subTest(grams=grams), self.assertRaises(UserError):
                prepare(args("--mode", "food", "--food", "毛豆", "--grams", grams), CONFIG, CATALOG)

    def test_unknown_and_duplicate(self):
        for cat in [[], CATALOG + [CATALOG[0]]]:
            with self.assertRaises(UserError):
                prepare(args("--mode", "food", "--food", "毛豆", "--grams", "60"), CONFIG, cat)

    def test_food_name_cannot_inject_formula(self):
        cells, _ = prepare(args("--mode", "kcal", "--food", '=IMPORTXML("url","x")', "--kcal", "1"), CONFIG, [])
        self.assertIn("stringValue", cells[3]["userEnteredValue"])

    def test_reject_ambiguous_modes(self):
        with self.assertRaises(UserError):
            prepare(args("--mode", "kcal", "--food", "餐", "--kcal", "100", "--grams", "100"), CONFIG, [])

    def test_bounded_reads(self):
        for span in ["A:A", "A1:ZZ9999", "B2:A1", "A0", "'log'!A1"]:
            with self.assertRaises(UserError):
                range_name(span)
        self.assertEqual(range_name("a1:m40"), "A1:M40")


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {**CONFIG, "state_dir": self.temp.name}
        self.book = Book()
        self.args = args("--mode", "food", "--food", "毛豆", "--grams", "60", "--commit")

    def tearDown(self):
        self.temp.cleanup()

    def test_append_confirm_and_dedupe(self):
        result = add(self.book, self.config, self.args)
        self.assertTrue(result["written"])
        self.assertEqual(result["receipt"]["row"], 386)
        self.assertTrue(add(self.book, self.config, self.args)["duplicate"])
        self.assertEqual(len(self.book.writes), 1)

    def test_lost_response_retry_is_safe(self):
        self.book.fail_after_commit = True
        self.assertEqual(add(self.book, self.config, self.args)["status"], "write_unknown")
        self.assertTrue(add(self.book, self.config, self.args)["duplicate"])
        self.assertEqual(len(self.book.writes), 1)

    def test_changed_content_same_id_rejected(self):
        add(self.book, self.config, self.args)
        self.args.grams = "90"
        with self.assertRaises(UserError):
            add(self.book, self.config, self.args)

    def test_dry_run_does_not_write(self):
        self.args.commit = False
        result = add(self.book, self.config, self.args)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.book.writes)


if __name__ == "__main__":
    unittest.main()
