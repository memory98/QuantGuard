#!/usr/bin/env python3
"""
tests/test_analyze_returns_freshness.py — [fix41] 분석 벤치마크·종목 가격 신선도(#OPEN-BM).

검증 강도 V5(실캡처 골든): 아래 종가는 2026-09-29 야후(069500.KS)에서 실제로 받은 값을
그대로 박아둔 것이다. mock을 테스트 대상 코드에서 베끼지 않았다(CLAUDE.md 목킹 원칙 1·2번).
동시에 V2(변이 구별): fix41 이전 코드는 아래 사고 재현 테스트를 **통과하지 못한다** —
구버전은 낡은 종가로 계산한 수치를 확정치처럼 float 하나로만 돌려줬기 때문이다.

재현하는 실사고(2026-09-28 주간분석):
  야후가 09-22·09-23 바를 아직 주지 않은 시점에 실행 → price_on_or_before(09-28)이
  09-11 종가(109,500)로 조용히 폴백 → 09-21→09-28 벤치 -1.97%(상대 +2.30%p, "시장 이김").
  40분 뒤 재실행 → 09-23 종가(113,145) 반영 → 벤치 +1.29%(상대 -0.96%p, "시장에 뒤짐").
  3.26%p 차이로 부호가 뒤집혔고, 경고는 한 줄도 없었다.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_returns as ar  # noqa: E402

# [골든] 2026-09-29 야후 069500.KS 실캡처 종가 (KODEX 200)
GOLDEN_CLOSES = {
    "2026-09-11": 109500.0,
    "2026-09-14": 105410.0,
    "2026-09-15": 104275.0,
    "2026-09-16": 106365.0,
    "2026-09-17": 106280.0,
    "2026-09-18": 109285.0,
    "2026-09-21": 111700.0,
    "2026-09-22": 111880.0,
    "2026-09-23": 113145.0,
    # 09-24·09-25는 바 자체가 없었고(휴장), 09-28 종가는 분석 시점에 미게시였다.
}


def series(dates):
    """주어진 날짜들만 담은 종가 시리즈(골든 값 사용)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.Series([GOLDEN_CLOSES[d] for d in dates], index=idx)


class FakeBenchmark(ar.BenchmarkFetcher):
    """야후 호출 없이 주어진 시리즈로 동작하는 벤치마크(네트워크 격리)."""

    def __init__(self, prices):
        super().__init__()
        self._prices = prices

    def fetch_range(self, start, end):
        return self


def run(date_str, equity):
    return {"updated_at": f"{date_str} 14:20:15", "total_equity_checked": equity}


class TestPricePointFreshness(unittest.TestCase):

    def test_exact_bar_is_not_stale(self):
        s = series(["2026-09-21", "2026-09-22", "2026-09-23"])
        p = ar.last_close_on_or_before(s, datetime(2026, 9, 23, 14, 20))
        self.assertTrue(p.is_exact)
        self.assertEqual(p.stale_days, 0)
        self.assertEqual(p.value, 113145.0)

    def test_missing_requested_day_is_marked_stale(self):
        """09-28 종가가 없으면 09-23으로 대체되지만 is_exact=False로 드러나야 한다."""
        s = series(["2026-09-21", "2026-09-22", "2026-09-23"])
        p = ar.last_close_on_or_before(s, datetime(2026, 9, 28, 14, 20))
        self.assertFalse(p.is_exact)
        self.assertEqual(p.as_of.date(), datetime(2026, 9, 23).date())
        self.assertEqual(p.stale_days, 5)

    def test_no_bar_at_all_returns_none(self):
        s = series(["2026-09-21"])
        self.assertIsNone(ar.last_close_on_or_before(s, datetime(2026, 9, 1)))


class TestIncidentReplay(unittest.TestCase):
    """2026-09-28 사고 재현 — 두 시점의 야후 응답이 다른 답을 내던 상황."""

    def _compute(self, price_series):
        runs = [run("2026-09-21", 2675172), run("2026-09-28", 2684048)]
        analyzer = ar.ReturnAnalyzer(runs, FakeBenchmark(price_series), {})
        analyzer._if_invested = lambda a, b: (None, [])   # 이 테스트는 벤치마크만 본다
        return analyzer.compute()[0]

    def test_truncated_feed_is_flagged_not_silently_wrong(self):
        """첫 실행 재현: 09-22·09-23 바가 아직 없어 09-11로 폴백 → 반드시 잠정 표기."""
        r = self._compute(series(["2026-09-11", "2026-09-14", "2026-09-21"]))
        self.assertTrue(r["provisional"], "낡은 종가로 계산했는데 확정치로 보고됐다(#OPEN-BM 재발)")
        self.assertIn("2026-09-28", r["provisional_reason"])
        self.assertEqual(r["benchmark_asof"]["to"], "2026-09-21")

    def test_settled_feed_still_provisional_until_that_days_close_exists(self):
        """재실행 재현: 09-23까지 채워져도 09-28 종가 자체가 없으면 여전히 잠정."""
        r = self._compute(series(["2026-09-14", "2026-09-21", "2026-09-22", "2026-09-23"]))
        self.assertTrue(r["provisional"])
        self.assertEqual(r["benchmark_return_pct"], 1.29)
        self.assertEqual(r["benchmark_asof"]["to"], "2026-09-23")

    def test_two_runs_disagree_but_both_are_flagged(self):
        """핵심: 어느 쪽 수치가 나오든 '확정'으로 기록되지 않는다."""
        early = self._compute(series(["2026-09-11", "2026-09-14", "2026-09-21"]))
        later = self._compute(series(["2026-09-14", "2026-09-21", "2026-09-22", "2026-09-23"]))
        self.assertNotEqual(early["benchmark_return_pct"], later["benchmark_return_pct"])
        self.assertTrue(early["provisional"] and later["provisional"])

    def test_no_false_positive_when_that_days_close_exists(self):
        """회귀 반대방향(정밀도): 요청일 종가가 있으면 잠정이 아니어야 한다.

        09-28 종가가 게시된 상황을 가정(114,000 가상값 — 존재 여부만 검증하므로 값은 무관).
        """
        s = series(["2026-09-21", "2026-09-22", "2026-09-23"])
        s = pd.concat([s, pd.Series([114000.0], index=pd.DatetimeIndex([pd.Timestamp("2026-09-28")]))])
        r = self._compute(s)
        self.assertFalse(r["provisional"])
        self.assertIsNone(r["provisional_reason"])


class TestIfInvestedFreshness(unittest.TestCase):

    def test_stale_stock_prices_flag_the_interval(self):
        """개별 종목 종가가 요청일 것이 아니면 '매수했으면/가드효과'도 잠정이다."""
        runs = [run("2026-09-21", 2675172), run("2026-09-28", 2684048)]
        bench = series(["2026-09-21", "2026-09-22", "2026-09-23"])
        bench = pd.concat([bench, pd.Series([114000.0],
                          index=pd.DatetimeIndex([pd.Timestamp("2026-09-28")]))])
        analyzer = ar.ReturnAnalyzer(runs, FakeBenchmark(bench), {})
        analyzer._if_invested = lambda a, b: (0.0093, ["0167A0", "367760"])
        r = analyzer.compute()[0]
        self.assertTrue(r["provisional"])
        self.assertIn("0167A0", r["provisional_reason"])
        self.assertEqual(r["if_invested_pct"], 0.93)


class TestSeriesValidation(unittest.TestCase):
    """시계열 자체가 망가졌으면 조용히 쓰지 말고 터져야 한다(fail-safe)."""

    def test_feed_stopped_long_ago_is_rejected(self):
        fetcher = ar.BenchmarkFetcher()
        old = series(["2026-09-11"])
        import unittest.mock as mock
        with mock.patch.object(ar.yf, "download", return_value=pd.DataFrame({"Close": old})):
            with self.assertRaises(RuntimeError) as cm:
                fetcher.fetch_range(datetime(2026, 6, 29), datetime(2026, 11, 30))
        self.assertIn("검증 실패", str(cm.exception))

    def test_empty_response_is_rejected(self):
        fetcher = ar.BenchmarkFetcher()
        import unittest.mock as mock
        with mock.patch.object(ar.yf, "download", return_value=pd.DataFrame()):
            with self.assertRaises(RuntimeError):
                fetcher.fetch_range(datetime(2026, 6, 29), datetime(2026, 9, 28))


if __name__ == "__main__":
    unittest.main()


class TestLateAnalysisRegression(unittest.TestCase):
    """[fix43] 분석을 며칠 늦게 돌려도 죽지 않아야 한다(fix41 회귀).

    시세는 분석 마지막 구간일(end)보다 최신인 것이 정상이다. as_of=end로 검증하면
    그 정상 상황을 '미래 데이터(시계 오류)'로 오판해 RuntimeError가 난다.
    """

    def test_series_newer_than_analysis_end_is_accepted(self):
        import unittest.mock as mock
        prices = series(["2026-09-21", "2026-09-22", "2026-09-23"])
        fetcher = ar.BenchmarkFetcher()
        with mock.patch.object(ar.yf, "download",
                               return_value=pd.DataFrame({"Close": prices})):
            fetcher.fetch_range(datetime(2026, 6, 29), datetime(2026, 9, 21))
        pt = fetcher.price_on_or_before(datetime(2026, 9, 21, 14, 20))
        self.assertTrue(pt.is_exact)
        self.assertEqual(pt.value, 111700.0)
