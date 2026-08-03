#!/usr/bin/env python3
"""
tests/test_reinvest.py — [감사 fix27] 잔여현금 재투입 수학 불변식 (💰💰💰 실돈).

집행부 매수/재투입 수학은 실돈을 직접 움직이는데 단위테스트가 0개였다(검증 V0).
reinvest_leftover_cash의 두 핵심 불변식을 V4(속성) 로 핀 박는다:
  ① 과투입 금지: 실제 매수 총액 <= 투입 전 잔여현금 (없는 돈으로 사지 않음)
  ② 종목당 상한: 각 종목 최종 평가액 <= kr_budget * REINVEST_CAP_RATIO (쏠림 방지)
경계 포함(<=)·price<=0 방어·현금 부족 케이스도 확인.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import korea  # noqa: E402
from config import REINVEST_CAP_RATIO  # noqa: E402


def run_reinvest(leftover, candidates, kr_budget):
    """execute_order/sleep을 무력화하고 reinvest_leftover_cash 실행 → orders 반환."""
    name_map = {c["code"]: c["code"] for c in candidates}
    with mock.patch.object(korea, "execute_order", return_value=True), \
         mock.patch.object(korea.time, "sleep", lambda *_: None):
        return korea.reinvest_leftover_cash(
            "tok", float(leftover), candidates, float(kr_budget), name_map)


class TestReinvestInvariants(unittest.TestCase):
    def _init_value(self, candidates):
        return {c["code"]: c["qty_total"] * c["limit_price"] for c in candidates}

    def test_never_overcommits_and_respects_cap(self):
        """②③ 여러 시나리오 스윕 — 과투입 금지 + 종목당 상한(경계 포함)."""
        kr_budget = 1_000_000
        cap = kr_budget * REINVEST_CAP_RATIO
        scenarios = [
            # (leftover, [(code, qty_total, price)])
            (130_000, [("A", 9, 10_000), ("B", 9, 10_000)]),
            (300_000, [("A", 0, 12_000), ("B", 0, 7_000), ("C", 0, 5_000)]),
            (9_000,   [("A", 9, 10_000)]),                       # 최저가보다 적은 잔여
            (500_000, [("A", 14, 10_000)]),                      # 이미 상한 근처(140k)
        ]
        for leftover, spec in scenarios:
            with self.subTest(leftover=leftover, spec=spec):
                candidates = [{"code": c, "qty_total": q, "limit_price": p}
                              for c, q, p in spec]
                init_val = self._init_value(candidates)
                orders = run_reinvest(leftover, candidates, kr_budget)
                spent = sum(o["qty"] * o["limit_price"] for o in orders)
                # ① 과투입 금지
                self.assertLessEqual(spent, leftover,
                                     f"과투입: {spent} > 잔여 {leftover}")
                # ② 종목당 상한(경계 포함)
                bought = {o["code"]: o["qty"] for o in orders}
                for c in candidates:
                    final_val = init_val[c["code"]] + bought.get(c["code"], 0) * c["limit_price"]
                    self.assertLessEqual(final_val, cap + 1e-6,
                                         f"{c['code']} 상한 초과: {final_val} > {cap}")

    def test_zero_price_skipped(self):
        """price<=0 종목은 재투입 대상에서 제외(나눗셈/무한매수 방지)."""
        candidates = [{"code": "A", "qty_total": 1, "limit_price": 0},
                      {"code": "B", "qty_total": 1, "limit_price": 10_000}]
        orders = run_reinvest(500_000, candidates, 1_000_000)
        codes = {o["code"] for o in orders}
        self.assertNotIn("A", codes)

    def test_insufficient_leftover_no_orders(self):
        """최저가보다 잔여가 적으면 주문 0건."""
        candidates = [{"code": "A", "qty_total": 5, "limit_price": 10_000}]
        orders = run_reinvest(3_000, candidates, 1_000_000)
        self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
