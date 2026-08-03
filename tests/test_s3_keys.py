#!/usr/bin/env python3
"""tests/test_s3_keys.py — 테스트 실행 아카이브 격리 (fix22)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
from s3_keys import archive_keys  # noqa: E402


class TestArchiveKeys(unittest.TestCase):
    def test_regular_run_uses_real_prefix(self):
        a, u = archive_keys("2026-08-03", force_bull=False)
        self.assertEqual(a, "quant_signals/2026-08-03.json")
        self.assertEqual(u, "universe/2026-08-03.json")

    def test_force_bull_isolated_to_test_prefix(self):
        # 테스트 실행은 실이력을 덮어쓰면 안 됨 → *_test/
        a, u = archive_keys("2026-08-03", force_bull=True)
        self.assertEqual(a, "quant_signals_test/2026-08-03.json")
        self.assertEqual(u, "universe_test/2026-08-03.json")
        self.assertNotIn("quant_signals/2026", a)  # 실아카이브 경로 아님


if __name__ == "__main__":
    unittest.main(verbosity=2)
