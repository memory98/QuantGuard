#!/usr/bin/env python3
"""
tests/test_vix_freshness.py — [fix25] VIX 신선도 가드 회귀 방지.

핵심 사고 시나리오: 야후 VIX 피드가 stale(며칠 지연)인데 그 값이 낮으면(옛날 평온장 값),
fetch_vix가 그걸 그대로 현재 VIX로 써서 BULL을 낸다. 미국발 폭락(VIX 급등이 나야 하는데
피드가 stale)에서 BEAR 대피가 해제되는 fail-open. 가드는 stale이면 UNKNOWN을 반환해
핸들러가 carry-over/안전 스킵하게 한다(fail-safe).
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
import signal_generator as sg  # noqa: E402


def vix_df(last_date, value=20.0, n=10):
    """yf.py 단일티커 출력 모사: 'Close' 열 + 일별 인덱스."""
    idx = pd.bdate_range(end=last_date, periods=n)
    return pd.DataFrame({"Close": [value] * n}, index=idx)


class TestVixFreshness(unittest.TestCase):
    def setUp(self):
        self.last_friday = datetime(2026, 7, 31)  # 금요일

    def test_fresh_low_vix_bull(self):
        df = vix_df(self.last_friday, value=20.0)
        with mock.patch.object(sg.yf, "download", return_value=df):
            vix, status = sg.fetch_vix(self.last_friday)
        self.assertEqual(status, "BULL")
        self.assertEqual(vix, 20.0)

    def test_fresh_high_vix_bear(self):
        df = vix_df(self.last_friday, value=35.0)
        with mock.patch.object(sg.yf, "download", return_value=df):
            vix, status = sg.fetch_vix(self.last_friday)
        self.assertEqual(status, "BEAR")

    def test_stale_vix_returns_unknown(self):
        # 마지막 바가 last_friday보다 10일 전 → stale > 5일 → UNKNOWN (값이 낮아도 BULL 금지)
        df = vix_df(self.last_friday - timedelta(days=10), value=15.0)
        with mock.patch.object(sg.yf, "download", return_value=df):
            vix, status = sg.fetch_vix(self.last_friday)
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(vix)

    def test_abnormal_value_returns_unknown(self):
        # 최신가 0/음수(야후 결측) → UNKNOWN
        df = vix_df(self.last_friday, value=0.0)
        with mock.patch.object(sg.yf, "download", return_value=df):
            vix, status = sg.fetch_vix(self.last_friday)
        self.assertEqual(status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
