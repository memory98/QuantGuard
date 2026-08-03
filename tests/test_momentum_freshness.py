#!/usr/bin/env python3
"""
tests/test_momentum_freshness.py — [fix26] 모멘텀 current 신선도 가드(#OPEN-2b).

검증 강도 V4(경계 스윕): stale=0..10일을 훑어 임계(5일) 경계에서 포함/제외가 뒤집히는지
확인한다(한 입력이 아니라 다수 입력에 대한 불변식). 거래정지/야후 종목별 stale로 옛날
가격의 모멘텀이 top10에 끼어 실매수되는 것을 막는지 회귀 방지.
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

LAST_FRIDAY = datetime(2026, 7, 31)


def stock_df(tickers, stale_days=0):
    """일별 종가 DF. 마지막 유효 바를 last_friday - stale_days 로 만든다(그 뒤는 NaN)."""
    idx = pd.date_range(end=LAST_FRIDAY, periods=400, freq="D")
    cutoff = pd.Timestamp(LAST_FRIDAY - timedelta(days=stale_days))
    data = {}
    for t in tickers:
        vals = [10000.0 + i for i in range(len(idx))]
        s = pd.Series(vals, index=idx)
        s[s.index > cutoff] = float("nan")   # cutoff 이후 결측 → 마지막 유효 = cutoff
        data[t] = s
    return pd.DataFrame(data, index=idx)


def one_stock_etf():
    return pd.DataFrame({"Code": ["000001"], "Name": ["ETF000001"]})


class TestMomentumFreshnessBoundary(unittest.TestCase):
    """V4 경계 스윕: 임계 5일 — 이하는 포함, 초과는 제외."""

    def test_stale_boundary_sweep(self):
        for stale, expect_scored in [(0, 1), (3, 1), (5, 1), (6, 0), (8, 0), (10, 0)]:
            with self.subTest(stale_days=stale):
                df = stock_df(["000001.KS"], stale_days=stale)
                with mock.patch.object(sg.yf, "download", return_value=df), \
                     mock.patch.object(sg._time, "sleep", lambda *_: None):
                    scores = sg.calc_momentum_scores(one_stock_etf(), LAST_FRIDAY)
                self.assertEqual(
                    len(scores), expect_scored,
                    f"stale={stale}일 → 기대 스코어수 {expect_scored}, 실제 {len(scores)}")

    def test_stale_stock_excluded_fresh_kept(self):
        """혼합 유니버스: 신선 종목은 남고 stale 종목만 빠진다."""
        etf = pd.DataFrame({"Code": ["000001", "000002"],
                            "Name": ["FRESH", "STALE"]})
        fresh = stock_df(["000001.KS"], stale_days=0)
        stale = stock_df(["000002.KS"], stale_days=10)
        combined = pd.concat([fresh, stale], axis=1)
        with mock.patch.object(sg.yf, "download", return_value=combined), \
             mock.patch.object(sg._time, "sleep", lambda *_: None):
            scores = sg.calc_momentum_scores(etf, LAST_FRIDAY)
        codes = {s["code"] for s in scores}
        self.assertIn("000001", codes)
        self.assertNotIn("000002", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
