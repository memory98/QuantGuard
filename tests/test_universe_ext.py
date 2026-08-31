#!/usr/bin/env python3
"""tests/test_universe_ext.py — 유니버스 확장(해외 편입) 후보 [섀도우 전용]

이 후보가 답하려는 질문:
  2026-06~08 구간에 KODEX200 -26%인데 SPY +4.3%였다. 국내 상장 해외지수 ETF를
  모멘텀 유니버스에 넣으면(= EXCLUDE_KEYWORDS의 해외 차단 12개만 해제) 분산이 생기는가?

여기서 검증하는 것은 **수익성이 아니라 계약**이다 — 프로덕션과 같은 모멘텀 정의를
쓰는가, 섹터 상한이 의도대로 걸리는가, 조회 실패가 원장을 죽이지 않는가.
수익성 판정은 원장 누적으로만 하며 이 테스트의 관심사가 아니다.

네트워크는 타지 않는다(fdr 조회는 전부 스텁).
"""
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))

import universe_ext as ux                                      # noqa: E402
from universe_ext import (                                     # noqa: E402
    ForeignEtfDiscovery, ForeignAugmenter, foreign_sector, FOREIGN_BLOCK)
from signal_generator import EXCLUDE_KEYWORDS, LOOKBACK        # noqa: E402
from base import sector_capped                                 # noqa: E402


class FakePrices:
    def __init__(self, series: dict):
        self._s = series

    def series(self, code):
        return self._s.get(code)


def ramp(start, n, step, first="2026-01-01"):
    idx = pd.bdate_range(first, periods=n)
    return pd.Series([start + i * step for i in range(n)], index=idx)


class TestForeignBlockList(unittest.TestCase):
    """해제 대상 키워드가 실제로 프로덕션 제외 목록의 부분집합인가."""

    def test_block_list_is_subset_of_production_excludes(self):
        self.assertTrue(set(FOREIGN_BLOCK) <= set(EXCLUDE_KEYWORDS),
                        msg="해외 차단 키워드가 프로덕션 목록과 어긋났다")

    def test_non_foreign_excludes_remain(self):
        """레버리지·인버스·채권 등은 절대 풀리면 안 된다."""
        keep = [k for k in EXCLUDE_KEYWORDS if k not in FOREIGN_BLOCK]
        for must in ("레버리지", "인버스", "채권", "달러", "KOFR"):
            self.assertIn(must, keep)


class TestForeignSector(unittest.TestCase):
    def test_buckets(self):
        cases = {
            "TIGER 미국S&P500": "해외_미국지수",
            "KODEX 미국나스닥100": "해외_미국기술",
            "TIGER 일본니케이225": "해외_선진",
            "KODEX 차이나항셍테크": "해외_신흥",
            "TIME 글로벌AI인공지능액티브": "해외_글로벌테마",
        }
        for name, bucket in cases.items():
            self.assertEqual(foreign_sector(name), bucket, msg=name)

    def test_unknown_falls_back(self):
        self.assertEqual(foreign_sector("이름없는해외ETF"), "해외_기타")


class TestAugmenterSectorModes(unittest.TestCase):
    """'single'은 섹터 상한 때문에 최대 1종목만 들어와야 한다 — 이게 설계 의도다."""

    def setUp(self):
        self.names = {"A": "TIGER 미국S&P500", "B": "KODEX 미국나스닥100",
                      "C": "TIGER 일본니케이225"}
        self.prices = FakePrices({c: ramp(100, 400, 0.3) for c in self.names})
        self.as_of = self.prices.series("A").index[-1].to_pydatetime()

    def test_single_mode_collapses_to_one_bucket(self):
        picks = ForeignAugmenter(self.names, mode="single").score(self.as_of, self.prices)
        self.assertEqual({p["sector"] for p in picks}, {"해외"})
        self.assertEqual(len(sector_capped(picks, 10, per_sector=1)), 1)

    def test_split_mode_allows_multiple(self):
        picks = ForeignAugmenter(self.names, mode="split").score(self.as_of, self.prices)
        self.assertEqual(len({p["sector"] for p in picks}), 3)
        self.assertEqual(len(sector_capped(picks, 10, per_sector=1)), 3)

    def test_domestic_sector_wins_when_theme_overlaps(self):
        """'글로벌HBM반도체'류는 국내 반도체와 같은 테마다 — 버킷이 갈리면 안 된다.

        2026-07-20 사고(같은 테마가 다른 버킷으로 갈려 동시 편입)의 재발 방지.
        """
        aug = ForeignAugmenter({"X": "KODEX 글로벌반도체"}, mode="split")
        self.assertEqual(aug._sector("KODEX 글로벌반도체"), "반도체")


class TestMomentumMatchesProduction(unittest.TestCase):
    """모멘텀 정의가 signal_generator와 같은가 — 다르면 후보 비교가 무의미해진다."""

    def test_base_is_first_trading_day_after_target(self):
        s = ramp(100, 400, 1.0)
        as_of = s.index[-1].to_pydatetime()
        aug = ForeignAugmenter({"A": "TIGER 미국S&P500"}, mode="split")
        got = aug.score(as_of, FakePrices({"A": s}))[0]

        base_date = as_of - timedelta(days=int(LOOKBACK * 7 / 5))
        exp_base = float(s[s.index >= pd.Timestamp(base_date)].iloc[0])
        exp_cur = float(s[s.index <= pd.Timestamp(as_of)].iloc[-1])
        self.assertAlmostEqual(got["momentum"], exp_cur / exp_base - 1, places=6)
        self.assertEqual(got["base_price"], round(exp_base, 0))

    def test_new_listing_is_excluded(self):
        """base 기준일 근방 데이터가 없으면 제외(프로덕션 fix9와 동일)."""
        s = ramp(100, 20, 1.0, first="2026-08-01")     # 최근 20일치뿐
        as_of = s.index[-1].to_pydatetime()
        aug = ForeignAugmenter({"A": "TIGER 미국S&P500"}, mode="split")
        self.assertEqual(aug.score(as_of, FakePrices({"A": s})), [])

    def test_missing_price_is_skipped_not_crash(self):
        aug = ForeignAugmenter({"A": "TIGER 미국S&P500"}, mode="split")
        self.assertEqual(aug.score(datetime(2026, 8, 28), FakePrices({})), [])


class TestAugment(unittest.TestCase):
    def test_domestic_kept_and_no_duplicate_codes(self):
        dom = [{"code": "069500", "name": "KODEX 200", "momentum": 0.1,
                "sector": "국내_대형지수"},
               {"code": "A", "name": "중복코드", "momentum": 0.2, "sector": "기타"}]
        s = ramp(100, 400, 0.3)
        aug = ForeignAugmenter({"A": "TIGER 미국S&P500", "B": "KODEX 미국나스닥100"},
                               mode="split")
        out = aug.augment(dom, s.index[-1].to_pydatetime(), FakePrices({"A": s, "B": s}))
        codes = [o["code"] for o in out]
        self.assertEqual(len(codes), len(set(codes)))          # 중복 없음
        self.assertEqual(out[1]["name"], "중복코드")            # 국내 쪽이 남는다
        self.assertIn("B", codes)

    def test_foreign_entries_are_flagged(self):
        s = ramp(100, 400, 0.3)
        out = ForeignAugmenter({"B": "KODEX 미국나스닥100"}, mode="split").augment(
            [], s.index[-1].to_pydatetime(), FakePrices({"B": s}))
        self.assertTrue(out[0]["foreign"])


class TestDiscovery(unittest.TestCase):
    """편입 규칙이 프로덕션과 같은가 — 해외만 추가되고 국내는 그대로여야 한다."""

    LISTING = pd.DataFrame([
        {"Code": "069500", "Name": "KODEX 200",          "amonut": 900},
        {"Code": "360750", "Name": "TIGER 미국S&P500",    "amonut": 800},
        {"Code": "133690", "Name": "TIGER 미국나스닥100",  "amonut": 700},
        {"Code": "122630", "Name": "KODEX 레버리지",       "amonut": 999},
        {"Code": "114800", "Name": "KODEX 인버스",         "amonut": 990},
        {"Code": "000001", "Name": "KODEX 국채30년",       "amonut": 980},
        {"Code": "117700", "Name": "KODEX 건설",          "amonut": 100},
    ])

    def _discover(self, cutoff):
        with unittest.mock.patch.object(ux.fdr, "StockListing",
                                        return_value=self.LISTING.copy()):
            return ForeignEtfDiscovery(cutoff=cutoff).discover()

    def test_only_foreign_names_are_added(self):
        newly = self._discover(cutoff=3)
        self.assertEqual(set(newly["Name"]), {"TIGER 미국S&P500", "TIGER 미국나스닥100"})

    def test_leverage_inverse_bond_never_added(self):
        newly = self._discover(cutoff=10)
        for bad in ("레버리지", "인버스", "국채"):
            self.assertFalse(any(bad in n for n in newly["Name"]), msg=bad)

    # 원본 순서와 거래대금 순서가 **어긋나도록** 짠 목록.
    #   원본순   : KODEX200(900) → 미국S&P500(10, 최하위) → 건설(100) → 미국나스닥100(800)
    #   거래대금순: KODEX200(900) → 미국나스닥100(800) → 건설(100) → 미국S&P500(10)
    # cutoff=2면 nlargest는 나스닥100을, head는 S&P500을 편입한다 → 두 방식을 구분한다.
    SKEWED = pd.DataFrame([
        {"Code": "069500", "Name": "KODEX 200",          "amonut": 900},
        {"Code": "360750", "Name": "TIGER 미국S&P500",    "amonut": 10},
        {"Code": "117700", "Name": "KODEX 건설",          "amonut": 100},
        {"Code": "133690", "Name": "KODEX 미국나스닥100",  "amonut": 800},
    ])

    def test_uses_turnover_not_listing_order(self):
        """fix15 교훈 — 네이버 원본 순서(시총순)가 아니라 거래대금으로 잘라야 한다.

        원본 순서로 자르면 거래대금 10짜리 사실상 무거래 ETF가 편입된다. 그런 종목을
        실제로 매수하면 체결이 안 되거나 슬리피지가 터진다 — 지금 조사 중인
        주당 -0.6%p 마찰(#OPEN-COST)과 같은 뿌리다.
        """
        with unittest.mock.patch.object(ux.fdr, "StockListing",
                                        return_value=self.SKEWED.copy()):
            newly = ForeignEtfDiscovery(cutoff=2).discover()
        self.assertEqual(set(newly["Name"]), {"KODEX 미국나스닥100"},
                         msg="거래대금이 아니라 원본 순서로 잘랐다(fix15 회귀)")


class TestPipelineFailSafe(unittest.TestCase):
    """조회가 깨져도 원장 전체가 죽으면 안 된다."""

    def test_discovery_failure_skips_candidates_only(self):
        import shadow_forward as sf
        with unittest.mock.patch.object(
                sf.ForeignEtfDiscovery, "discover",
                side_effect=RuntimeError("네이버 장애")):
            a, b, codes = sf._build_augmenters()
        self.assertIsNone(a)
        self.assertIsNone(b)
        self.assertEqual(codes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
