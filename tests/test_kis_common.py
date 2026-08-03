#!/usr/bin/env python3
"""
tests/test_kis_common.py — 공통 함수 통합(fix23) + fetch_total_equity 하드닝 테스트.

핵심: identity 테스트로 "단일 소스"를 못 박는다. 누가 korea/lambda_function에 로컬
     execute_order/calc_limit_price를 다시 정의하면 `is` 동일성이 깨져 테스트 실패.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import kis_common          # noqa: E402
import korea               # noqa: E402
import lambda_function     # noqa: E402


def fake_pool(response_dict):
    resp = mock.Mock()
    resp.data = json.dumps(response_dict).encode("utf-8")
    pm = mock.Mock()
    pm.request.return_value = resp
    return mock.Mock(return_value=pm)


class TestSingleSource(unittest.TestCase):
    """중복 제거 검증 — 세 모듈이 동일 함수 객체를 참조해야 함(갈라짐 불가)."""

    def test_execute_order_is_shared(self):
        self.assertIs(korea.execute_order, kis_common.execute_order)
        self.assertIs(lambda_function.execute_order, kis_common.execute_order)

    def test_price_helpers_shared(self):
        self.assertIs(korea.calc_limit_price, kis_common.calc_limit_price)
        self.assertIs(korea.get_tick_size, kis_common.get_tick_size)
        self.assertIs(lambda_function.calc_limit_price, kis_common.calc_limit_price)


class TestCalcLimitPrice(unittest.TestCase):
    def test_sell_down_buy_up(self):
        self.assertEqual(kis_common.calc_limit_price(10000, -0.01), 9900)
        self.assertEqual(kis_common.calc_limit_price(10000, 0.0), 10005)
        self.assertEqual(kis_common.get_tick_size(999), 5)


class TestFetchTotalEquity(unittest.TestCase):
    """fix23: rt_cd 검증 + 재시도 (korea fix14와 동일 하드닝)."""

    def test_parses_on_ok(self):
        ok = {"rt_cd": "0", "output2": [{"tot_evlu_amt": "2712844"}]}
        with mock.patch.object(lambda_function.urllib3, "PoolManager", fake_pool(ok)):
            self.assertEqual(lambda_function.fetch_total_equity("tok"), 2712844)

    def test_abnormal_retries_then_raises(self):
        # rt_cd!=0 이면 조용히/즉시 실패 금지 → 재시도 후 예외 (fix14 하드닝이 여기도 적용됐나)
        bad = {"rt_cd": "1", "msg1": "유량초과", "output2": []}
        with mock.patch.object(lambda_function.urllib3, "PoolManager", fake_pool(bad)), \
             mock.patch.object(lambda_function.time, "sleep", lambda *_: None):
            with self.assertRaises(Exception):
                lambda_function.fetch_total_equity("tok", max_retries=2)

    def test_rejects_error_rtcd_even_with_data(self):
        # 핵심 회귀: rt_cd 오류인데 output2에 값이 있는 응답을 그대로 반환하면 안 됨
        # (구버전은 output2만 보고 999를 반환했음 → 오류 무시). 하드닝은 rt_cd 검증 → 예외.
        bad = {"rt_cd": "1", "output2": [{"tot_evlu_amt": "999"}]}
        with mock.patch.object(lambda_function.urllib3, "PoolManager", fake_pool(bad)), \
             mock.patch.object(lambda_function.time, "sleep", lambda *_: None):
            with self.assertRaises(Exception):
                lambda_function.fetch_total_equity("tok", max_retries=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
