#!/usr/bin/env python3
"""
tests/test_universe_coverage.py — [fix24] 유니버스 커버리지 가드 회귀 방지.

핵심 사고 시나리오: 100종목을 50개씩 2배치로 받는데 한 배치가 통째로 실패하면,
기존 코드는 나머지 50종목만으로 top10을 뽑아 반쪽 유니버스로 실전 매수했다.
가드는 스코어된 종목이 요청 유니버스의 70% 미만이면 []를 반환해 fail-safe로 끝낸다
(핸들러 500 → 시그널 미갱신 → Lambda B가 STALE_SIGNAL_ABORT로 포지션 유지).
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
import signal_generator as sg  # noqa: E402


def make_close_df(tickers):
    """yf.download 형태 모사: 'Close' 레벨 없이 티커 열을 가진 일별 종가 DF.

    코드가 `if "Close" in raw.columns else raw` 로 분기하므로 티커 열만 있으면 됨.
    base_date(≈176일 전) 근방까지 데이터가 있도록 400 영업일치를 만든다.
    """
    idx = pd.bdate_range(end="2026-08-01", periods=400)
    data = {t: [10000.0 + i for i in range(len(idx))] for t in tickers}
    return pd.DataFrame(data, index=idx)


def make_etf_df(n=100):
    codes = [f"{i:06d}" for i in range(n)]
    return pd.DataFrame({"Code": codes, "Name": [f"ETF{c}" for c in codes]})


class TestUniverseCoverageGuard(unittest.TestCase):
    def setUp(self):
        self.etf_df = make_etf_df(100)  # 50*2 = 두 배치
        self.last_friday = datetime(2026, 8, 1)

    def test_full_coverage_scores(self):
        """두 배치 모두 성공 → 정상 스코어링(비어있지 않음)."""
        def fake_download(batch, *a, **k):
            return make_close_df(batch)

        with mock.patch.object(sg.yf, "download", side_effect=fake_download), \
             mock.patch.object(sg._time, "sleep", lambda *_: None):
            scores = sg.calc_momentum_scores(self.etf_df, self.last_friday)
        self.assertTrue(len(scores) > 0)
        # 커버리지 100% 이므로 요청 100종목이 대부분 스코어돼야 함
        self.assertGreaterEqual(len(scores), 90)

    def test_partial_batch_failure_aborts(self):
        """첫 배치 통째 실패(50/100=50% < 70%) → fail-safe로 [] 반환."""
        calls = {"n": 0}

        def fake_download(batch, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("배치1 yfinance 장애")
            return make_close_df(batch)

        with mock.patch.object(sg.yf, "download", side_effect=fake_download), \
             mock.patch.object(sg._time, "sleep", lambda *_: None):
            scores = sg.calc_momentum_scores(self.etf_df, self.last_friday)
        self.assertEqual(scores, [], "반쪽 유니버스는 중단(fail-safe)해야 함")


if __name__ == "__main__":
    unittest.main(verbosity=2)
