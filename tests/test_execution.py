#!/usr/bin/env python3
"""
tests/test_execution.py — 실행경로(rambdaB) 단위테스트
========================================================
실돈을 잃는 버그(fix14 자산0·fix17 잔고필드·fix18 매수가능)를 자동으로 잡는다.

⚠️ mock 필드명 근거(CLAUDE.md 목킹 원칙): 테스트 대상 코드에서 베끼지 않고,
   log/2026-07-13_kis_api_full_audit.json 이 koreainvestment/open-trading-api 공식
   예제로 검증한 필드명 + dashboard/app.py 독립 사용처를 근거로 사용.
   → 공식필드: 잔고 hldg_qty·pdno·prpr·tot_evlu_amt(TTTC8434R),
              매수가능 nrcvb_buy_amt·ord_psbl_cash·ruse_psbl_amt(TTTC8908R).
   코드가 이 필드를 못 읽게 회귀하면(예: hldg_qty→hldn_qty) 테스트가 실패한다.

실행: dashboard/.venv/bin/python -m unittest tests.test_execution -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import korea  # noqa: E402


def fake_pool(response_dict):
    """urllib3.PoolManager 패치용: request()가 response_dict를 JSON으로 반환."""
    resp = mock.Mock()
    resp.data = json.dumps(response_dict).encode("utf-8")
    pm = mock.Mock()
    pm.request.return_value = resp
    return mock.Mock(return_value=pm)


class TestLimitPrice(unittest.TestCase):
    """주문 지정가 로직(5원 틱). 틱 버그 = 주문거부/오체결."""

    def test_tick_is_5(self):
        self.assertEqual(korea.get_tick_size(12345), 5)

    def test_sell_rounds_down_to_tick(self):
        # BEAR 매도 -1%: 내림, 5원 단위
        self.assertEqual(korea.calc_limit_price(10000, -0.01), 9900)   # raw 9900
        self.assertEqual(korea.calc_limit_price(10007, -0.01), 9905)   # raw 9906.93→9905
        self.assertEqual(korea.calc_limit_price(50000, -0.01), 49500)

    def test_buy_rounds_up_by_tick(self):
        # 매수(rate>=0): 체결 확보 위해 한 틱 올림
        self.assertEqual(korea.calc_limit_price(10000, 0.0), 10005)
        self.assertEqual(korea.calc_limit_price(10006, 0.0), 10010)

    def test_never_below_one_tick(self):
        self.assertGreaterEqual(korea.calc_limit_price(1, -0.01), 5)


class TestFetchHoldings(unittest.TestCase):
    """잔고 파싱(TTTC8434R). fix17(hldg_qty)·fix14(재시도/예외) 회귀 방지."""

    OK = {
        "rt_cd": "0",
        "output1": [
            {"pdno": "069500", "hldg_qty": "10", "prpr": "8500", "prdt_name": "KODEX 200"},
            {"pdno": "005930", "hldg_qty": "0", "prpr": "70000", "prdt_name": "삼성전자"},  # 0주=제외
            {"pdno": "", "hldg_qty": "5", "prpr": "100", "prdt_name": "코드없음"},          # 코드없음=제외
        ],
        "output2": [{"tot_evlu_amt": "2712844"}],
    }

    def test_parses_holdings_and_total(self):
        with mock.patch.object(korea.urllib3, "PoolManager", fake_pool(self.OK)):
            holdings, total = korea.fetch_present_holdings("tok")
        self.assertEqual(total, 2712844)
        self.assertIn("069500", holdings)
        self.assertEqual(holdings["069500"]["qty"], 10)          # hldg_qty를 못 읽으면 0 → 실패
        self.assertEqual(holdings["069500"]["prpr"], 8500)
        self.assertNotIn("005930", holdings)                     # 0주 제외
        self.assertEqual(len(holdings), 1)                       # 코드없음도 제외

    def test_abnormal_response_raises_not_silent_zero(self):
        # fix14: rt_cd!=0 이면 조용히 0원 반환 금지 → 예외
        bad = {"rt_cd": "1", "msg1": "유량초과", "output2": []}
        with mock.patch.object(korea.urllib3, "PoolManager", fake_pool(bad)), \
             mock.patch.object(korea.time, "sleep", lambda *_: None):
            with self.assertRaises(Exception):
                korea.fetch_present_holdings("tok", max_retries=2)


class TestFetchAvailableCash(unittest.TestCase):
    """매수가능 파싱(TTTC8908R). fix18(nrcvb_buy_amt) 회귀 방지."""

    def test_uses_nrcvb_buy_amt(self):
        resp = {"rt_cd": "0", "output": {
            "nrcvb_buy_amt": "2712844", "ord_psbl_cash": "1000000", "ruse_psbl_amt": "500000"}}
        with mock.patch.object(korea.urllib3, "PoolManager", fake_pool(resp)):
            self.assertEqual(korea.fetch_available_cash("tok"), 2712844)  # 필드 오류면 0

    def test_fallback_when_nrcvb_zero(self):
        resp = {"rt_cd": "0", "output": {
            "nrcvb_buy_amt": "0", "ord_psbl_cash": "1000000", "ruse_psbl_amt": "500000"}}
        with mock.patch.object(korea.urllib3, "PoolManager", fake_pool(resp)):
            self.assertEqual(korea.fetch_available_cash("tok"), 1500000)

    def test_abnormal_returns_zero(self):
        resp = {"rt_cd": "1", "msg1": "오류"}
        with mock.patch.object(korea.urllib3, "PoolManager", fake_pool(resp)):
            self.assertEqual(korea.fetch_available_cash("tok"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
