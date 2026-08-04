#!/usr/bin/env python3
"""
tests/test_rebalancing_e2e.py — [감사 fix28] 메인 리밸런싱 사이징 e2e (#OPEN-E2E).

run_korea_rebalancing 본체(target_qty·노트레이드 밴드·예수금 부족 축소·순위 이탈 매도·
히스테리시스 유지)는 💰💰💰 실돈 경로인데 e2e 테스트가 없었다(검증 V0). S3/잔고/시세/주문을
전부 목킹한 하네스로 L2(경계 수량)·L4(매도/매수 분기 상태)를 검증한다.

불변식:
  ① 신규 진입 균등배분: qty == int(budget_per // price)
  ② 매수 총액 <= 가용 예수금 (없는 돈으로 사지 않음)
  ③ 순위 이탈(15위 밖) 보유 → 전량 매도 / 11~15위 → 히스테리시스 유지(매도 안 함)
"""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import korea  # noqa: E402
from config import BUDGET_RATIO, NUM_TARGETS  # noqa: E402


def make_signal(codes_prices, market_status="BULL", candidates=None):
    """codes_prices: [(code, signal_price)] → top_10_stocks 시그널 dict."""
    top = [{"code": c, "name": c, "price": p} for c, p in codes_prices]
    sig = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": market_status,
        "top_10_stocks": top,
    }
    if candidates is not None:
        sig["candidates"] = candidates
    return sig


class Harness:
    """run_korea_rebalancing의 외부 의존을 전부 목킹한 실행기."""
    def __init__(self, signal, holdings, total_asset, cash, price_map):
        self.signal = signal
        self.holdings = holdings          # {code: {"qty","prpr","name"}}
        self.total_asset = total_asset
        self.cash = cash
        self.price_map = price_map        # {code: realtime_price}
        self.orders = []                  # execute_order 호출 기록

    def _exec(self, token, code, qty, is_buy, limit_price=0):
        self.orders.append({"code": code, "qty": qty, "is_buy": is_buy,
                            "limit_price": limit_price})
        return True

    def run(self):
        body = mock.Mock()
        body.read.return_value = json.dumps(self.signal).encode("utf-8")
        s3 = mock.Mock()
        s3.get_object.return_value = {"Body": body}
        with mock.patch.object(korea, "boto3") as m_boto, \
             mock.patch.object(korea, "check_market_open", return_value=True), \
             mock.patch.object(korea, "fetch_present_holdings",
                               return_value=(self.holdings, self.total_asset)), \
             mock.patch.object(korea, "fetch_available_cash", return_value=self.cash), \
             mock.patch.object(korea, "get_realtime_price",
                               side_effect=lambda tok, code: self.price_map.get(code, 0)), \
             mock.patch.object(korea, "wait_sell_settlement", return_value=True), \
             mock.patch.object(korea, "execute_order", side_effect=self._exec), \
             mock.patch.object(korea.time, "sleep", lambda *_: None):
            m_boto.client.return_value = s3
            return korea.run_korea_rebalancing("tok", fallback_total_equity=self.total_asset)

    @property
    def buys(self):
        return [o for o in self.orders if o["is_buy"]]

    @property
    def sells(self):
        return [o for o in self.orders if not o["is_buy"]]


class TestRebalancingSizing(unittest.TestCase):
    def test_new_entry_equal_weight_sizing(self):
        """① 10종목 신규 균등배분: 각 qty == int(budget_per // price), ② 총액 <= cash."""
        codes_prices = [(f"S{i:02d}", 10_000) for i in range(10)]
        signal = make_signal(codes_prices)
        price_map = {c: 10_000 for c, _ in codes_prices}
        total_asset = 2_000_000
        budget_per = total_asset * BUDGET_RATIO / NUM_TARGETS  # 200,000
        expected_qty = int(budget_per // 10_000)               # 20
        h = Harness(signal, holdings={}, total_asset=total_asset,
                    cash=5_000_000, price_map=price_map)
        res = h.run()
        self.assertEqual(res["result"], "BULL_REBALANCING_SUCCESS")
        # 종목별 1차 매수(코드별 첫 주문)만 균등배분 수량을 검증한다.
        # (남은 현금 재투입 fix15은 같은 종목에 1주씩 추가 주문을 낼 수 있음 → 별도)
        first_buy = {}
        for o in h.buys:
            first_buy.setdefault(o["code"], o)
        self.assertEqual(len(first_buy), 10, "10종목 모두 1차 매수돼야 함")
        for code, o in first_buy.items():
            self.assertEqual(o["qty"], expected_qty, f"{code} 1차 수량 오류")
        # 재투입 포함 전체 매수 총액도 가용 예수금을 넘지 않아야 한다(과지출 금지).
        spent = sum(o["qty"] * o["limit_price"] for o in h.buys)
        self.assertLessEqual(spent, 5_000_000)

    def test_insufficient_cash_never_overspends(self):
        """② 예수금이 빠듯해도 실제 매수 총액이 가용 예수금을 넘지 않는다(축소/스킵)."""
        codes_prices = [(f"S{i:02d}", 10_000) for i in range(10)]
        signal = make_signal(codes_prices)
        price_map = {c: 10_000 for c, _ in codes_prices}
        cash = 500_000
        h = Harness(signal, holdings={}, total_asset=2_000_000,
                    cash=cash, price_map=price_map)
        h.run()
        spent = sum(o["qty"] * o["limit_price"] for o in h.buys)
        self.assertLessEqual(spent, cash, f"과지출: {spent} > {cash}")
        self.assertTrue(len(h.buys) >= 1)

    def test_rank_exit_sells_hysteresis_holds(self):
        """③ 15위 밖 보유 → 전량 매도 / 11~15위 보유 → 히스테리시스 유지(매도 안 함)."""
        codes_prices = [(f"T{i:02d}", 10_000) for i in range(10)]
        # 후보풀 rank: T00~T09=1~10위, HYS=12위(유지), EXIT는 candidates에 없음(15위 밖)
        candidates = [{"code": c, "rank": i + 1} for i, (c, _) in enumerate(codes_prices)]
        candidates.append({"code": "HYS", "rank": 12})
        signal = make_signal(codes_prices, candidates=candidates)
        price_map = {c: 10_000 for c, _ in codes_prices}
        price_map["HYS"] = 10_000
        price_map["EXIT"] = 10_000
        holdings = {
            "HYS":  {"qty": 5, "prpr": 10_000, "name": "HYS"},
            "EXIT": {"qty": 7, "prpr": 10_000, "name": "EXIT"},
        }
        h = Harness(signal, holdings=holdings, total_asset=2_000_000,
                    cash=5_000_000, price_map=price_map)
        h.run()
        sold_codes = {o["code"] for o in h.sells}
        self.assertIn("EXIT", sold_codes, "15위 밖 보유는 전량 매도해야 함")
        self.assertNotIn("HYS", sold_codes, "11~15위는 히스테리시스 유지(매도 금지)")
        # EXIT은 전량(7주) 매도
        exit_order = next(o for o in h.sells if o["code"] == "EXIT")
        self.assertEqual(exit_order["qty"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
