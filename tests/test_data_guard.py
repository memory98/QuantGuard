#!/usr/bin/env python3
"""tests/test_data_guard.py — 시세 신선도·정합성 가드 테스트 (fix22)."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
from data_guard import validate_prices  # noqa: E402

ASOF = datetime(2026, 8, 3)


class TestValidatePrices(unittest.TestCase):
    def test_valid(self):
        ok, _ = validate_prices(datetime(2026, 8, 1), 200, 99000, ASOF, min_rows=20)
        self.assertTrue(ok)

    def test_insufficient_rows(self):
        ok, r = validate_prices(datetime(2026, 8, 1), 10, 99000, ASOF, min_rows=20)
        self.assertFalse(ok)
        self.assertIn("부족", r)

    def test_zero_or_none_value(self):
        self.assertFalse(validate_prices(datetime(2026, 8, 1), 200, 0, ASOF, min_rows=20)[0])
        self.assertFalse(validate_prices(datetime(2026, 8, 1), 200, None, ASOF, min_rows=20)[0])
        self.assertFalse(validate_prices(datetime(2026, 8, 1), 200, -5, ASOF, min_rows=20)[0])

    def test_stale_data(self):
        # 10일 지연 > max 5 → 실패
        ok, r = validate_prices(datetime(2026, 7, 24), 200, 99000, ASOF, min_rows=20, max_stale_days=5)
        self.assertFalse(ok)
        self.assertIn("낡음", r)

    def test_fresh_within_limit(self):
        # 3일 지연 ≤ 5 → 통과 (주말 갭 허용)
        ok, _ = validate_prices(datetime(2026, 7, 31), 200, 99000, ASOF, min_rows=20, max_stale_days=5)
        self.assertTrue(ok)

    def test_future_data_rejected(self):
        ok, r = validate_prices(datetime(2026, 8, 10), 200, 99000, ASOF, min_rows=20)
        self.assertFalse(ok)
        self.assertIn("미래", r)

    def test_missing_date(self):
        self.assertFalse(validate_prices(None, 200, 99000, ASOF, min_rows=20)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
