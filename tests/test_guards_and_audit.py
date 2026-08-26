#!/usr/bin/env python3
"""tests/test_guards_and_audit.py — 2026-08-23 관측 2종

① backtest/guards.py + shadow_forward 후보 추가
   - 가장 중요한 것은 **기존 후보의 성적이 한 자리도 안 바뀌는가**다. 관측을 늘리려다
     과거 원장을 조용히 흔들면 OOS 판정 근거 자체가 무너진다.
② rambdaB/execution_audit.py — 주문 '접수'와 '체결'의 분리 관측(#OPEN-1)
   - 이 모듈의 계약은 "무슨 일이 있어도 매매를 죽이지 않는다"이므로 실패 경로를 집중 검증.

⚠️ 외부 API 목킹 한계 (CLAUDE.md 원칙 3):
   잔고조회 응답 mock의 `hldg_qty`/`pdno`는 **검증된 필드**다 — fix17 실사고로 교정됐고
   dashboard/app.py가 같은 API를 같은 이름으로 쓰고 있다(저장소 내 독립 근거 존재).
   반면 `pchs_avg_pric`는 저장소 어디에도 사용처가 없고 실캡처도 없다. 그래서 이 테스트는
   "값이 있으면 읽고, 없거나 이상하면 None으로 두고 넘어간다"는 **동작만** 검증하며
   필드명 자체는 검증하지 않는다. 진짜 필드명은 다음 실전 실행의
   execution_audit.balance_output1_keys 로 확인한다.
"""
import sys
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))
sys.path.insert(0, str(ROOT / "rambdaB"))

from guards import (  # noqa: E402
    DDGuard, ComboGuard, SigmaDDGuard, DailyCircuitBreaker, K_SIGMA, NORMAL_SIGMA,
    SMABreakGuard, AnyOf, AllOf, Not, GuardVerdict,
    K_SIGMA_RECAL, NORMAL_SIGMA_ROLLING)
from costs import CostModel, split_turnover, DEFAULT_COST  # noqa: E402
from shadow_forward import portfolio_return  # noqa: E402
from signal_generator import DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD  # noqa: E402
from execution_audit import ExecutionAuditor, run_execution_audit  # noqa: E402

D = lambda s: datetime.strptime(s, "%Y-%m-%d")  # noqa: E731


class FakePrices:
    """PriceProvider 최소 대역 — kodex() / at() 만 쓴다."""

    def __init__(self, kodex: pd.Series, stocks: dict = None):
        self._k = kodex
        self._s = stocks or {}

    def kodex(self):
        return self._k

    def at(self, code, date):
        s = self._s.get(code)
        if s is None:
            return None
        sub = s[s.index <= pd.Timestamp(date)].dropna()
        return float(sub.iloc[-1]) if len(sub) else None


def flat_then(values, start="2026-01-01"):
    """앞쪽을 평평한 100으로 채워 lookback을 만족시킨 뒤 values를 잇는다."""
    pad = [100.0] * (DD_GUARD_LOOKBACK + 40)
    data = pad + list(values)
    idx = pd.bdate_range(start=start, periods=len(data))
    return pd.Series(data, index=idx)


class TestDDGuardUnchanged(unittest.TestCase):
    """현행 가드를 객체로 옮기면서 판정이 달라지지 않았는가."""

    def test_bear_when_below_threshold(self):
        s = flat_then([100.0, 100.0, 85.0])          # 20일 고점 100 대비 -15%
        g = DDGuard()
        self.assertEqual(g.status(FakePrices(s), s.index[-1]), "BEAR")

    def test_bull_when_above_threshold(self):
        s = flat_then([100.0, 100.0, 95.0])          # -5% → 임계 -8% 위
        self.assertEqual(DDGuard().status(FakePrices(s), s.index[-1]), "BULL")

    def test_matches_production_expression_across_boundary_sweep(self):
        """프로덕션 수식(`dd <= threshold`)과 경계 부근에서 한 건도 어긋나지 않는가.

        '정확히 -8%'를 부동소수점으로 만들 수는 없다(100*0.92/100-1 = -0.0799...).
        그래서 특정 값이 BEAR인지를 묻는 대신, 경계를 촘촘히 훑으며 **가드 객체의 판정이
        프로덕션 표현식과 항상 같은지**를 본다. 이쪽이 리팩터링 회귀에 대한 진짜 계약이다.
        """
        g = DDGuard()
        for pct in [-0.0805, -0.0801, -0.08, -0.0799, -0.0795, -0.05, -0.20, 0.0]:
            s = flat_then([100.0, 100.0, 100.0 * (1 + pct)])
            sub = s.tail(DD_GUARD_LOOKBACK)
            dd = float(s.iloc[-1] / sub.max() - 1)          # 프로덕션과 동일한 계산
            expected = "BEAR" if dd <= DD_GUARD_THRESHOLD else "BULL"
            self.assertEqual(g.status(FakePrices(s), s.index[-1]), expected,
                             msg=f"pct={pct} dd={dd!r}")

    def test_threshold_comparison_is_inclusive(self):
        """`dd <= threshold`의 **등호**를 직접 겨냥한다(L2 경계 버그 클래스).

        경계 스윕만으로는 부족하다 — 부동소수점상 정확히 -8%가 되는 가격을 만들 수 없어
        `<=`를 `<`로 바꿔도 스윕은 전부 통과한다(2026-08-23 변이 테스트로 실측 확인).
        그래서 가격을 맞추는 대신 **실제 계산된 dd를 그대로 임계로 주입**해
        '같을 때 BEAR인가'를 묻는다. `<`로 바뀌면 이 테스트는 반드시 깨진다.
        """
        s = flat_then([100.0, 100.0, 91.7])
        dd = float(s.iloc[-1] / s.tail(DD_GUARD_LOOKBACK).max() - 1)
        self.assertEqual(DDGuard(threshold=dd).status(FakePrices(s), s.index[-1]), "BEAR")
        # 임계가 dd보다 아주 조금이라도 아래면 BULL이어야 한다(반대 방향 고정)
        self.assertEqual(DDGuard(threshold=dd * 1.0001).status(FakePrices(s), s.index[-1]),
                         "BULL")

    def test_insufficient_history_is_bull(self):
        s = pd.Series([100.0, 99.0], index=pd.bdate_range("2026-01-01", periods=2))
        self.assertEqual(DDGuard().status(FakePrices(s), s.index[-1]), "BULL")


class TestComboGuard(unittest.TestCase):
    def test_dd_ok_but_below_sma_is_bear(self):
        # 완만하게 계속 내려 SMA120 아래지만 20일 낙폭은 -8% 미만인 구간
        vals = [100.0 - i * 0.12 for i in range(200)]
        s = pd.Series(vals, index=pd.bdate_range("2026-01-01", periods=len(vals)))
        p = FakePrices(s)
        self.assertEqual(DDGuard().status(p, s.index[-1]), "BULL")
        self.assertEqual(ComboGuard().status(p, s.index[-1]), "BEAR")


class TestSigmaDDGuard(unittest.TestCase):
    """임계가 변동성에 따라 실제로 넓어지는가 + 계수가 역산값 그대로인가."""

    def test_k_is_derived_not_handpicked(self):
        expected = abs(DD_GUARD_THRESHOLD) / (NORMAL_SIGMA * (DD_GUARD_LOOKBACK ** 0.5))
        self.assertAlmostEqual(K_SIGMA, expected, places=10)

    def test_recal_k_is_derived_not_handpicked(self):
        """재보정 K도 손으로 고른 값이 아니라 역산값인가(원본 K와 동일한 규율)."""
        expected = abs(DD_GUARD_THRESHOLD) / (NORMAL_SIGMA_ROLLING * (DD_GUARD_LOOKBACK ** 0.5))
        self.assertAlmostEqual(K_SIGMA_RECAL, expected, places=10)

    def test_recal_k_is_looser_than_original(self):
        """재보정판은 임계가 더 넓다(= 덜 민감). 두 계열이 실제로 다른 후보임을 고정."""
        self.assertGreater(K_SIGMA_RECAL, K_SIGMA)
        s = flat_then([100.0] * 30)
        p = FakePrices(s)
        # 같은 시계열에서 재보정판 임계가 더 음수(넓음)여야 한다
        th_o = SigmaDDGuard().threshold_at(p, s.index[-1])
        th_r = SigmaDDGuard(k=K_SIGMA_RECAL).threshold_at(p, s.index[-1])
        if th_o is not None and th_r is not None:
            self.assertLess(th_r, th_o)

    def test_describe_distinguishes_k_variant(self):
        """원장에서 두 계열이 구분되는가 — 안 그러면 나란히 채점해도 사후 식별 불가."""
        self.assertIn("original", SigmaDDGuard().describe()["k_variant"])
        self.assertIn("recalibrated",
                      SigmaDDGuard(k=K_SIGMA_RECAL).describe()["k_variant"])

    def test_threshold_widens_with_volatility(self):
        calm = flat_then([100.0 + (1 if i % 2 else -1) * 0.3 for i in range(40)])
        wild = flat_then([100.0 + (1 if i % 2 else -1) * 6.0 for i in range(40)])
        g = SigmaDDGuard()
        th_calm = g.threshold_at(FakePrices(calm), calm.index[-1])
        th_wild = g.threshold_at(FakePrices(wild), wild.index[-1])
        self.assertIsNotNone(th_calm)
        self.assertIsNotNone(th_wild)
        self.assertLess(th_wild, th_calm)   # 더 음수 = 더 넓다

    def test_threshold_comparison_is_inclusive(self):
        """σ가드도 `dd <= th`의 등호를 고정한다(DDGuard와 동일한 L2 클래스).

        동적 임계라 가격으로는 경계를 맞출 수 없으므로, threshold_at이 실제 dd를
        그대로 돌려주도록 바꿔 '같을 때 BEAR인가'를 묻는다.
        """
        s = flat_then([100.0, 100.0, 80.0])
        dd = float(s.iloc[-1] / s.tail(DD_GUARD_LOOKBACK).max() - 1)
        g = SigmaDDGuard()
        with unittest.mock.patch.object(SigmaDDGuard, "threshold_at", lambda *_: dd):
            self.assertEqual(g.status(FakePrices(s), s.index[-1]), "BEAR")
        with unittest.mock.patch.object(SigmaDDGuard, "threshold_at",
                                        lambda *_: dd * 1.0001):
            self.assertEqual(g.status(FakePrices(s), s.index[-1]), "BULL")

    def test_falls_back_to_fixed_when_sigma_unavailable(self):
        """σ 계산 불가(표본 부족)면 프로덕션 고정 임계로 폴백 — fail 방향 동일."""
        s = pd.Series([100.0, 90.0], index=pd.bdate_range("2026-01-01", periods=2))
        p = FakePrices(s)
        self.assertEqual(SigmaDDGuard().status(p, s.index[-1]),
                         DDGuard().status(p, s.index[-1]))

    def test_describe_reports_actual_vol_window(self):
        """describe()가 실제 vol_window를 보고하는가.

        원장(AUDIT ③ STEP B)은 describe()를 그대로 박아 사후 변경을 막는 장치다.
        여기서 표본길이를 고정 문자열로 적으면 vol_window를 바꿔 스윕할 때
        원장에 거짓 파라미터가 남아 장치 자체가 무력화된다.
        표본길이(vol_window)와 낙폭 지평(DD_GUARD_LOOKBACK)은 별개이므로,
        지평은 vol_window와 무관하게 항상 DD_GUARD_LOOKBACK이어야 한다.
        """
        self.assertIn(f"sigma{DD_GUARD_LOOKBACK}", SigmaDDGuard().describe()["formula"])
        for vw in (10, 63):
            d = SigmaDDGuard(vol_window=vw).describe()
            self.assertEqual(d["vol_window"], vw)
            self.assertIn(f"sigma{vw}", d["formula"])
            self.assertIn(f"sqrt({DD_GUARD_LOOKBACK})", d["formula"])

    def test_high_vol_crash_stays_bull_where_fixed_goes_bear(self):
        """고변동 국면에서 -10% 낙폭: 고정 임계는 BEAR, σ 임계는 아직 BULL."""
        noisy = [100.0 + (1 if i % 2 else -1) * 5.0 for i in range(40)]
        s = flat_then(noisy + [max(noisy) * 0.90])
        p = FakePrices(s)
        self.assertEqual(DDGuard().status(p, s.index[-1]), "BEAR")
        self.assertEqual(SigmaDDGuard().status(p, s.index[-1]), "BULL")


class TestDailyCircuitBreaker(unittest.TestCase):
    def test_weekly_status_delegates_to_base(self):
        s = flat_then([100.0, 100.0, 85.0])
        p = FakePrices(s)
        self.assertEqual(DailyCircuitBreaker().status(p, s.index[-1]),
                         DDGuard().status(p, s.index[-1]))

    def test_emergency_threshold_is_double(self):
        g = DailyCircuitBreaker()
        self.assertAlmostEqual(g.emergency_threshold, DD_GUARD_THRESHOLD * 2.0)

    def test_intra_exit_fires_on_deep_drop(self):
        s = flat_then([100.0, 100.0, 100.0, 78.0, 95.0])   # -22% 하루
        g = DailyCircuitBreaker()
        hit = g.intra_exit(FakePrices(s), s.index[-5], s.index[-1])
        self.assertIsNotNone(hit)
        self.assertEqual(pd.Timestamp(hit), s.index[-2])

    def test_intra_exit_silent_on_shallow_dip(self):
        """2026-08-19 실측(-9.95%)에 해당하는 얕은 딥에서는 발동하지 않아야 한다."""
        s = flat_then([100.0, 100.0, 100.0, 90.05, 100.0])
        g = DailyCircuitBreaker()
        self.assertIsNone(g.intra_exit(FakePrices(s), s.index[-5], s.index[-1]))

    def test_other_guards_never_intra_exit(self):
        s = flat_then([100.0, 100.0, 50.0])
        for g in (DDGuard(), ComboGuard(), SigmaDDGuard()):
            self.assertIsNone(g.intra_exit(FakePrices(s), s.index[-3], s.index[-1]))


class TestGuardVerdict(unittest.TestCase):
    """판정 옆에 근거가 실려 오는가(gs-quant TriggerInfo 대응).

    없으면 원장에 '왜 대피했나'가 안 남아 매번 과거를 재생해야 한다.
    """

    def test_status_still_returns_plain_string(self):
        """기존 호출부 무변경 보장 — status()는 여전히 문자열이다."""
        s = flat_then([100.0, 100.0, 85.0])
        self.assertIsInstance(DDGuard().status(FakePrices(s), s.index[-1]), str)

    def test_verdict_carries_reason(self):
        s = flat_then([100.0, 100.0, 85.0])
        v = DDGuard().evaluate(FakePrices(s), s.index[-1])
        self.assertEqual(v.status, "BEAR")
        self.assertTrue(v.is_bear)
        self.assertAlmostEqual(v.reason["threshold"], DD_GUARD_THRESHOLD)
        self.assertLess(v.reason["dd"], 0)
        self.assertTrue(v.reason["fired"])

    def test_verdict_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            GuardVerdict("MAYBE")

    def test_combo_reason_names_which_condition_fired(self):
        """콤보가드가 DD 때문인지 SMA 때문인지 구분되는가."""
        s = flat_then([100.0, 100.0, 85.0])
        v = ComboGuard().evaluate(FakePrices(s), s.index[-1])
        self.assertEqual(v.status, "BEAR")
        self.assertIn("DD가드", [f["rule"] for f in v.reason["fired_by"]])


class TestCombinators(unittest.TestCase):
    """조합자가 기존 상속 구현과 같은 답을 내는가 + 클래스 추가 없이 후보를 만드는가."""

    def test_anyof_reproduces_combo_guard(self):
        """ComboGuard == AnyOf([DD, SMA]) — 여러 형태의 계열에서 판정이 일치."""
        cases = [
            flat_then([100.0, 100.0, 85.0]),        # DD 발동
            flat_then([100.0] * 5),                  # 둘 다 미발동
            flat_then([100.0, 100.0, 99.0]),         # 얕은 딥
        ]
        composed = AnyOf([DDGuard(), SMABreakGuard()])
        for s in cases:
            p = FakePrices(s)
            self.assertEqual(ComboGuard().status(p, s.index[-1]),
                             composed.status(p, s.index[-1]))

    def test_allof_is_stricter_than_anyof(self):
        s = flat_then([100.0, 100.0, 85.0])
        p = FakePrices(s)
        any_ = AnyOf([DDGuard(), SMABreakGuard()]).status(p, s.index[-1])
        all_ = AllOf([DDGuard(), SMABreakGuard()]).status(p, s.index[-1])
        self.assertEqual(any_, "BEAR")          # DD만으로 발동
        self.assertEqual(all_, "BULL")          # SMA는 미발동이므로 AND는 불성립

    def test_not_inverts(self):
        s = flat_then([100.0, 100.0, 85.0])
        p = FakePrices(s)
        self.assertEqual(DDGuard().status(p, s.index[-1]), "BEAR")
        self.assertEqual(Not(DDGuard()).status(p, s.index[-1]), "BULL")

    def test_empty_composite_rejected(self):
        with self.assertRaises(ValueError):
            AnyOf([])


class TestLedgerContract(unittest.TestCase):
    """기록한 것이 **실제로 파일에 저장되는가**.

    2026-08-26 사고: guard_reason을 `ledgers[name]["intervals"]`에만 넣었는데
    그 구조는 저장 대상이 아니어서, "원장에 대피 사유가 남는다"는 주장이 거짓이었다.
    메커니즘(evaluate가 근거를 반환하는가)만 테스트하고 **저장 경로**를 안 봐서 놓쳤다.
    """

    def _build(self, guard_reasons):
        from shadow_forward import build_ledger
        g = DDGuard()
        return build_ledger(
            src="universe",
            rows=[{"from": "2026-08-03", "to": "2026-08-10", "후보A": 0.0}],
            names=["후보A"],
            ledgers={"후보A": {"cum": 1.0}},
            specs=[("후보A", None, g)],
            guard_reasons=guard_reasons,
        )

    def test_saved_ledger_has_all_promised_keys(self):
        from shadow_forward import LEDGER_REQUIRED_KEYS
        led = self._build({})
        for k in LEDGER_REQUIRED_KEYS:
            self.assertIn(k, led, f"원장 계약 필드 누락: {k}")

    def test_guard_reason_actually_reaches_the_saved_ledger(self):
        """근거를 넣으면 저장 본문에서 되읽을 수 있어야 한다(이번 사고의 회귀 방지)."""
        reason = {"rule": "DD가드", "dd": -0.12, "threshold": -0.08, "fired": True}
        led = self._build({"2026-08-03→2026-08-10": {"후보A": reason}})
        got = led["guard_reasons"]["2026-08-03→2026-08-10"]["후보A"]
        self.assertEqual(got["rule"], "DD가드")
        self.assertAlmostEqual(got["dd"], -0.12)
        self.assertTrue(got["fired"])

    def test_ledger_records_cost_model(self):
        self.assertEqual(self._build({})["cost_model"]["round_trip_pct"], 0.4)


class TestIntervalDrawdown(unittest.TestCase):
    """AUDIT ③ STEP D의 폭락 사건 판정에 쓸 구간별 낙폭 최저치.

    guard_reasons는 BEAR 구간에만 남으므로, 사건(-16% 진입)을 세려면 별도 기록이 필요하다.
    """

    def _prices(self, series):
        class P:
            def kodex(self_inner):
                return series
        return P()

    def test_records_the_worst_drawdown_in_the_window(self):
        from shadow_forward import interval_dd_min
        s = flat_then([100.0] * 3 + [70.0, 90.0])   # 구간 중 -30% 찍고 반등
        dd = interval_dd_min(self._prices(s), s.index[-5], s.index[-1])
        self.assertIsNotNone(dd)
        # 최저치를 잡아야 한다 — 구간 끝값(-10%)이 아니라 바닥(-30%)
        self.assertLess(dd, -0.25)

    def test_returns_none_when_sample_insufficient(self):
        from shadow_forward import interval_dd_min
        s = pd.Series([100.0, 99.0], index=pd.bdate_range("2026-01-01", periods=2))
        self.assertIsNone(interval_dd_min(self._prices(s), s.index[0], s.index[-1]))

    def test_window_is_left_exclusive(self):
        """(d0, d1] — 리밸런싱일 자체는 직전 구간에서 이미 셌다."""
        from shadow_forward import interval_dd_min
        s = flat_then([100.0] * 4)
        self.assertIsNone(interval_dd_min(self._prices(s), s.index[-1], s.index[-1]))


class TestCostModel(unittest.TestCase):
    """비용 단일 소스 도입이 기존 수치를 바꾸지 않는가."""

    def test_symmetric_model_equals_old_formula(self):
        new_w, prev_w = {"a": 0.5, "b": 0.5}, {"a": 0.2, "c": 0.3}
        old_turn = sum(abs(new_w.get(c, 0) - prev_w.get(c, 0))
                       for c in set(new_w) | set(prev_w))
        buy, sell = split_turnover(new_w, prev_w)
        self.assertAlmostEqual(buy + sell, old_turn)
        self.assertAlmostEqual(DEFAULT_COST.on_turnover(buy, sell),
                               old_turn * 0.002)

    def test_asymmetric_costs_are_expressible(self):
        m = CostModel(entry=0.001, exit=0.003)
        self.assertFalse(m.is_symmetric)
        self.assertAlmostEqual(m.round_trip, 0.004)
        self.assertAlmostEqual(m.on_turnover(1.0, 0.0), 0.001)
        self.assertAlmostEqual(m.on_turnover(0.0, 1.0), 0.003)

    def test_negative_cost_rejected(self):
        with self.assertRaises(ValueError):
            CostModel(entry=-0.001)


class OneStock:
    """weights 고정 전략 스텁."""

    def __init__(self, weights, cash=0.0):
        self._w, self._c = weights, cash

    def build_portfolio(self, universe, market):
        if market.get("market_status") == "BEAR":
            return {"weights": {}, "cash": 1.0}
        return {"weights": dict(self._w), "cash": self._c}


class TestPortfolioReturnEquivalence(unittest.TestCase):
    """exit_date=None이면 기존 공식과 완전히 같은 값이어야 한다(회귀 방지 핵심)."""

    def setUp(self):
        idx = pd.bdate_range("2026-08-03", periods=10)
        self.prices = FakePrices(
            pd.Series([100.0] * 10, index=idx),
            {"A": pd.Series([100, 101, 102, 103, 104, 105, 106, 107, 108, 110.0], index=idx),
             "B": pd.Series([50, 49, 48, 47, 46, 45, 44, 43, 42, 40.0], index=idx)})
        self.d0, self.d1 = idx[0], idx[-1]
        self.strat = OneStock({"A": 0.5, "B": 0.5})
        self.market = {"market_status": "BULL"}

    def _legacy(self, prev_hold):
        """변경 전 공식을 그대로 옮겨 적은 참조 구현."""
        pf = self.strat.build_portfolio(None, self.market)
        per_r, usable = {}, {}
        for c, w in pf["weights"].items():
            p0, p1 = self.prices.at(c, self.d0), self.prices.at(c, self.d1)
            if p0 and p1 and p0 > 0:
                per_r[c], usable[c] = p1 / p0 - 1, w
        wsum = sum(usable.values())
        if wsum > 0:
            usable = {c: w / wsum for c, w in usable.items()}
        gross = sum(usable[c] * per_r[c] for c in usable) if usable else 0.0
        turn = sum(abs(usable.get(c, 0) - prev_hold.get(c, 0))
                   for c in set(usable) | set(prev_hold))
        return gross - turn * 0.002

    def test_matches_legacy_formula_from_cash(self):
        net, _, _, exited = portfolio_return(
            self.strat, None, self.market, self.prices, self.d0, self.d1, {})
        self.assertAlmostEqual(net, self._legacy({}), places=12)
        self.assertFalse(exited)

    def test_matches_legacy_formula_with_prior_holdings(self):
        prev = {"A": 0.5, "B": 0.5}
        net, _, _, exited = portfolio_return(
            self.strat, None, self.market, self.prices, self.d0, self.d1, prev)
        self.assertAlmostEqual(net, self._legacy(prev), places=12)
        self.assertFalse(exited)

    def test_exit_date_uses_midpoint_price_and_charges_liquidation(self):
        mid = self.prices._s["A"].index[4]
        net_full, _, _, _ = portfolio_return(
            self.strat, None, self.market, self.prices, self.d0, self.d1, {})
        net_exit, cash, drift, exited = portfolio_return(
            self.strat, None, self.market, self.prices, self.d0, self.d1, {}, mid)
        self.assertTrue(exited)
        self.assertEqual(drift, {})          # 청산 후엔 다음 구간 보유 없음
        self.assertEqual(cash, 1.0)
        self.assertNotAlmostEqual(net_full, net_exit)
        # 중간 청산분 = (중간까지 수익) - 진입회전비용 - 청산비용
        self.assertAlmostEqual(net_exit, (0.5 * (104 / 100 - 1) + 0.5 * (46 / 50 - 1))
                               - 1.0 * 0.002 - 1.0 * 0.002, places=12)

    def test_exit_date_ignored_when_portfolio_is_cash(self):
        """BEAR라 이미 현금이면 비상 청산은 아무 일도 하지 않는다."""
        mid = self.prices._s["A"].index[4]
        net, _, _, exited = portfolio_return(
            self.strat, None, {"market_status": "BEAR"}, self.prices,
            self.d0, self.d1, {}, mid)
        self.assertFalse(exited)
        self.assertAlmostEqual(net, 0.0, places=12)


# ── ② 체결 감사 ──────────────────────────────────────────────

class TestBalanceRequestSpec(unittest.TestCase):
    """fix33에서 잔고조회 요청 스펙을 함수로 추출했다. 그 내용이 안 변했는가.

    이 테스트가 없으면 위험하다 — `test_execution.py`의 잔고 테스트는 `PoolManager`를
    통째로 목킹해 **URL·헤더를 무시하고** 정해진 응답만 돌려준다. 즉 쿼리 파라미터를
    하나 오타 내도 전 테스트가 통과하고, 실전에서만 깨진다(fix17이 고쳤던 바로 그 사고).
    그래서 스펙 자체를 문자열 수준에서 못박는다.
    """

    def setUp(self):
        import korea
        self.url, self.headers = korea.balance_request_spec("TESTTOKEN")

    def test_endpoint_and_tr_id(self):
        self.assertIn("/uapi/domestic-stock/v1/trading/inquire-balance", self.url)
        self.assertEqual(self.headers["tr_id"], "TTTC8434R")

    def test_all_fix17_required_params_present(self):
        """fix17이 채워 넣은 필수 파라미터가 하나도 빠지지 않았는가."""
        for param in ("CANO=", "ACNT_PRDT_CD=", "AFHR_FLPR_YN=N", "OFL_YN=",
                      "INQR_DVSN=02", "UNPR_DVSN=01", "FUND_STTL_ICLD_YN=N",
                      "FNCG_AMT_AUTO_RDPT_YN=N", "PRCS_DVSN=00",
                      "CTX_AREA_FK100=", "CTX_AREA_NK100="):
            self.assertIn(param, self.url, msg=f"필수 파라미터 누락: {param}")

    def test_removed_bogus_params_stay_removed(self):
        """fix17이 제거한 '규격에 없는 이름'이 되살아나지 않았는가(변이-구별)."""
        for bogus in ("AFHR_FLG", "OVR_FLG"):
            self.assertNotIn(bogus, self.url)

    def test_auth_headers(self):
        self.assertEqual(self.headers["authorization"], "Bearer TESTTOKEN")
        self.assertIn("appkey", self.headers)
        self.assertIn("appsecret", self.headers)

    def test_is_single_source_used_by_fetch_present_holdings(self):
        """감사 모듈과 잔고조회가 같은 스펙을 쓰는지 구조로 못박는다.

        `fetch_present_holdings`가 스펙 함수를 실제로 호출하는지 확인 — 호출하지 않고
        자체 URL을 다시 만들면 fix23이 없앤 '중복 정의 → 수동 동기화 누락'이 부활한다.
        """
        import korea
        called = []
        real = korea.balance_request_spec
        with unittest.mock.patch.object(
                korea, "balance_request_spec",
                side_effect=lambda t: (called.append(t), real(t))[1]):
            with unittest.mock.patch.object(korea.urllib3, "PoolManager",
                                            self._fake_pool()):
                korea.fetch_present_holdings("TOK")
        self.assertEqual(called, ["TOK"])

    @staticmethod
    def _fake_pool():
        import json as _json

        class _Res:
            data = _json.dumps({
                "rt_cd": "0",
                "output1": [{"pdno": "069500", "hldg_qty": "1",
                             "prpr": "100", "prdt_name": "x"}],
                "output2": [{"tot_evlu_amt": "1000"}]}).encode()

        class _Pool:
            def request(self, *a, **k):
                return _Res()

        return lambda *a, **k: _Pool()


def spec_stub():
    return "http://example.invalid/balance", {"tr_id": "TTTC8434R"}


def audit_enabled():
    """fix34 킬 스위치는 기본 OFF다. 감사 동작 자체를 보는 테스트는 켜고 본다."""
    import execution_audit as ea
    return unittest.mock.patch.object(ea, "EXEC_AUDIT_ENABLED", True)


class TestExecutionAuditor(unittest.TestCase):
    """계약: 정확히 세거나, 조용히 포기하거나. 절대 예외를 위로 던지지 않는다."""

    def _auditor(self, items, before, fail=False):
        a = ExecutionAuditor("tok", spec_stub, poll_interval=0, max_polls=2)
        a.capture_before(before)

        def reader():
            if fail:
                raise RuntimeError("rt_cd=1 msg1=유량초과")
            return items
        a._read_balance_raw = reader
        return a

    def test_full_fill_detected(self):
        # hldg_qty/pdno = 검증된 필드(fix17 + dashboard/app.py)
        items = [{"pdno": "367760", "hldg_qty": "5", "pchs_avg_pric": "59700"}]
        a = self._auditor(items, {})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5,
                        "limit_price": 59885, "ok": True}])
        self.assertTrue(rep["ok"])
        o = rep["orders"][0]
        self.assertEqual((o["filled_qty"], o["fill_status"]), (5, "FULL"))
        self.assertEqual(o["avg_price"], 59700)
        self.assertFalse(o["avg_price_blended"])
        self.assertFalse(rep["avg_price_schema_verified"])   # 늘 False여야 한다

    def test_partial_fill_detected(self):
        items = [{"pdno": "367760", "hldg_qty": "2"}]
        a = self._auditor(items, {})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        o = rep["orders"][0]
        self.assertEqual((o["filled_qty"], o["fill_status"]), (2, "PARTIAL"))

    def test_no_fill_detected(self):
        """접수는 성공(ok=True)했지만 한 주도 안 늘었다 → NONE. 이게 #OPEN-1의 핵심."""
        a = self._auditor([], {})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertEqual(rep["orders"][0]["fill_status"], "NONE")

    def test_sell_fill_uses_negative_delta(self):
        a = self._auditor([], {"367760": {"qty": 5}})
        rep = a.audit([{"side": "SELL", "code": "367760", "qty": 5, "ok": True}])
        o = rep["orders"][0]
        self.assertEqual((o["filled_qty"], o["fill_status"]), (5, "FULL"))

    def test_added_position_marks_avg_price_blended(self):
        items = [{"pdno": "367760", "hldg_qty": "9", "pchs_avg_pric": "58000"}]
        a = self._auditor(items, {"367760": {"qty": 4}})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertTrue(rep["orders"][0]["avg_price_blended"])

    def test_reinvest_orders_are_summed_per_code(self):
        items = [{"pdno": "367760", "hldg_qty": "5"}]
        a = self._auditor(items, {})
        rep = a.audit([
            {"side": "BUY", "code": "367760", "qty": 4, "ok": True},
            {"side": "BUY", "code": "367760", "qty": 1, "ok": True, "reinvest": True}])
        self.assertEqual(len(rep["orders"]), 1)
        self.assertEqual(rep["orders"][0]["req_qty"], 5)
        self.assertEqual(rep["orders"][0]["fill_status"], "FULL")

    def test_rejected_orders_are_not_counted_as_fillable(self):
        a = self._auditor([], {})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": False}])
        self.assertEqual(rep["orders"], [])

    def test_missing_avg_price_field_is_none_not_crash(self):
        """미검증 필드가 없거나 이상해도 감사 전체가 죽으면 안 된다."""
        items = [{"pdno": "367760", "hldg_qty": "5", "pchs_avg_pric": "이상한값"}]
        a = self._auditor(items, {})
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertTrue(rep["ok"])
        self.assertIsNone(rep["orders"][0]["avg_price"])
        self.assertIsNone(rep["avg_price_field"])

    def test_schema_keys_recorded_for_self_verification(self):
        """다음 실전 실행에서 진짜 필드명을 추측 없이 알아내기 위한 장치(#OPEN-V)."""
        items = [{"pdno": "1", "hldg_qty": "1", "zzz": "1", "aaa": "2"}]
        rep = self._auditor(items, {}).audit(
            [{"side": "BUY", "code": "1", "qty": 1, "ok": True}])
        self.assertEqual(rep["balance_output1_keys"], ["aaa", "hldg_qty", "pdno", "zzz"])

    def test_balance_failure_is_reported_not_raised(self):
        a = self._auditor([], {}, fail=True)
        rep = a.audit([{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertFalse(rep["ok"])
        self.assertIn("BALANCE_READ_FAILED", rep["reason"])

    def test_no_orders_is_ok(self):
        rep = self._auditor([], {}).audit([])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["reason"], "NO_ORDERS")


class TestRunExecutionAuditNeverRaises(unittest.TestCase):
    """진입점 계약: 무엇이 터져도 매매 경로로 예외가 새어나가지 않는다."""

    def test_broken_spec_fn_is_swallowed(self):
        def boom():
            raise RuntimeError("스펙 함수 폭발")
        with audit_enabled():
            rep = run_execution_audit("tok", boom, {}, [{"side": "BUY", "code": "A",
                                                        "qty": 1, "ok": True}],
                                      poll_interval=0)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["orders"], [])

    def test_garbage_holdings_does_not_raise(self):
        with audit_enabled():
            rep = run_execution_audit("tok", spec_stub, {"A": "문자열"}, [], poll_interval=0)
        self.assertIsInstance(rep, dict)

    def test_force_test_mode_skips_audit_entirely(self):
        """모의 모드에선 잔고가 안 변하므로, 감사하면 전 종목이 가짜 '미체결'이 된다.

        네트워크도 타지 않아야 한다(spec_fn이 호출되면 실패하도록 심어 확인).
        """
        import execution_audit as ea

        def must_not_be_called():
            raise AssertionError("FORCE_TEST_MODE인데 잔고조회를 시도했다")

        with audit_enabled(), unittest.mock.patch.object(ea, "FORCE_TEST_MODE", True):
            rep = run_execution_audit("tok", must_not_be_called, {},
                                      [{"side": "BUY", "code": "A", "qty": 1, "ok": True}],
                                      poll_interval=0)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["reason"], "FORCE_TEST_MODE")
        self.assertEqual(rep["orders"], [])


class TestKillSwitchDefaultOff(unittest.TestCase):
    """[fix34] 기본 OFF일 때 fix33 이전과 실행 경로가 완전히 같은가.

    실환경 미검증 코드를 실매매 당일에 켜지 않기 위한 스위치다. '꺼져 있다'는 것은
    단순히 결과가 비었다는 뜻이 아니라 **잔고 재조회도 sleep도 하지 않는다**는 뜻이어야
    한다(그래야 실행시간이 그대로다). 그래서 둘 다 호출되면 실패하도록 심어 확인한다.
    """

    def setUp(self):
        import config
        self.cfg = config

    def test_default_is_off(self):
        """배포 기본값이 꺼짐인가 — 이게 뒤집히면 스위치의 의미가 없다."""
        self.assertFalse(self.cfg.EXEC_AUDIT_ENABLED)

    def test_disabled_makes_no_balance_call_and_no_sleep(self):
        import execution_audit as ea

        def must_not_be_called():
            raise AssertionError("꺼져 있는데 잔고조회를 시도했다")

        with unittest.mock.patch.object(ea, "EXEC_AUDIT_ENABLED", False), \
             unittest.mock.patch.object(ea.time, "sleep",
                                        side_effect=AssertionError("꺼져 있는데 sleep했다")):
            rep = run_execution_audit(
                "tok", must_not_be_called, {"367760": {"qty": 4}},
                [{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["reason"], "DISABLED")
        self.assertEqual(rep["orders"], [])

    def test_enabled_actually_audits(self):
        """스위치를 켜면 정상 동작한다 — OFF 테스트가 '항상 통과'가 아님을 보인다."""
        import execution_audit as ea
        auditor_items = [{"pdno": "367760", "hldg_qty": "5"}]
        with audit_enabled(), \
             unittest.mock.patch.object(ea.ExecutionAuditor, "_read_balance_raw",
                                        lambda self: auditor_items), \
             unittest.mock.patch.object(ea.time, "sleep", lambda *_: None):
            rep = run_execution_audit(
                "tok", spec_stub, {},
                [{"side": "BUY", "code": "367760", "qty": 5, "ok": True}])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["orders"][0]["fill_status"], "FULL")

    def test_env_var_parsing(self):
        """콘솔에서 켤 때 흔히 쓰는 표기를 모두 받아들이는가(대소문자·1/yes/on).

        importlib.reload를 쓰지 않는 이유: rambdaA/config.py 와 rambdaB/config.py 가
        **둘 다 `config`라는 이름으로** import돼 sys.modules에서 충돌한다(테스트 환경
        한정 — 실제 Lambda는 각자 자기 것만 패키징된다). 어느 쪽이 먼저 잡히느냐에 따라
        reload 대상이 달라지므로, 파일 경로를 명시해 rambdaB 것만 확실히 로드한다.
        """
        import importlib.util, os
        path = ROOT / "rambdaB" / "config.py"

        def load_with(raw):
            if raw is None:
                os.environ.pop("EXEC_AUDIT_ENABLED", None)
            else:
                os.environ["EXEC_AUDIT_ENABLED"] = raw
            spec = importlib.util.spec_from_file_location("_rb_config_probe", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.EXEC_AUDIT_ENABLED

        try:
            for raw, expected in [("true", True), ("TRUE", True), ("True", True),
                                  (" true ", True), ("1", True), ("yes", True),
                                  ("on", True), ("false", False), ("", False),
                                  ("0", False), ("아무말", False),
                                  (None, False)]:   # 미설정 = 기본 OFF
                self.assertEqual(load_with(raw), expected, msg=f"raw={raw!r}")
        finally:
            os.environ.pop("EXEC_AUDIT_ENABLED", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
