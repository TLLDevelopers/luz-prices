import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_tomorrow  # noqa: E402
import fetch_prices as fp  # noqa: E402

NOW = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)
# 2026-11-24 00:00 CET = 2026-11-23 23:00 UTC = 1795474800
T0 = 1795474800


def raw(n=96, start=T0, lic="CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"):
    return {"license_info": lic, "unix_seconds": [start + 900 * i for i in range(n)],
            "price": [10.0 + i for i in range(n)], "unit": "EUR / MWh"}


class FetchTests(unittest.TestCase):
    def test_validate_rejects_bad_license_and_lengths(self):
        with self.assertRaises(ValueError):
            fp.validate(raw(lic="private use only"))
        bad = raw(); bad["price"].pop()
        with self.assertRaises(ValueError):
            fp.validate(bad)
        out = raw(); out["price"][0] = 99999
        with self.assertRaises(ValueError):
            fp.validate(out)
        with self.assertRaises(ValueError):
            fp.validate({})

    def test_writes_day_and_index(self):
        with tempfile.TemporaryDirectory() as d:
            idx = fp.update_zone("DE", Path(d), NOW, fetcher=lambda *a: raw())
            doc = json.loads((Path(d) / "v1/DE/2026-11-24.json").read_text())
            self.assertEqual(doc["slots"], 96)
            self.assertEqual(doc["license"]["url"], "https://creativecommons.org/licenses/by/4.0/")
            self.assertEqual(doc["resolution_minutes"], 15)
            self.assertTrue(idx["tomorrow_available"])
            self.assertEqual(idx["status"], "ok")

    def test_failure_keeps_last_valid_and_marks_age(self):
        with tempfile.TemporaryDirectory() as d:
            fp.update_zone("AT", Path(d), NOW, fetcher=lambda *a: raw())

            def boom(*a):
                raise RuntimeError("down")
            later = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)
            idx = fp.update_zone("AT", Path(d), later, fetcher=boom)
            self.assertTrue((Path(d) / "v1/AT/2026-11-24.json").exists())
            self.assertEqual(idx["status"], "stale")
            self.assertGreater(idx["age_hours"], 40)
            self.assertIn("down", idx["last_error"])

    def test_no_downgrade_of_complete_day(self):
        with tempfile.TemporaryDirectory() as d:
            fp.update_zone("NL", Path(d), NOW, fetcher=lambda *a: raw(96))
            fp.update_zone("NL", Path(d), NOW, fetcher=lambda *a: raw(40))
            doc = json.loads((Path(d) / "v1/NL/2026-11-24.json").read_text())
            self.assertEqual(doc["slots"], 96)

    def test_alert_when_tomorrow_missing_after_deadline(self):
        st = {"zones": {"DE": {"status": "ok", "age_hours": 1, "last_error": None,
                               "tomorrow_available": False, "last_data_date": "2026-11-23"}}}
        late = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)  # 17:00 Berlin
        early = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(check_tomorrow.problems_for(st, late))
        self.assertFalse(check_tomorrow.problems_for(st, early))


if __name__ == "__main__":
    unittest.main()
