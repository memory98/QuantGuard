#!/usr/bin/env python3
"""tests/test_shadow_account.py — 섀도우 원장 실계좌 채점 정확도 (fix31).

두 결함의 회귀 방지:
  ① 진짜 0%를 결측으로 오분류 — BEAR 100% 현금 대피 중에는 자산이 정확히 같아도
     그것이 실제 0% 수익이다. 옛 판정은 `e1 != e0`(금액 비교)이라 이를 버렸다.
  ② 결측을 0%로 보고 — 채점된 구간이 하나도 없어도 acc_cum 초기값 1.0이 그대로
     "+0.00%"로 텔레그램에 나갔다(조용한 실패).
"""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))
sys.path.insert(0, str(ROOT / "scripts"))
from shadow_forward import AccountReader  # noqa: E402
from notify_telegram_shadow import render_account  # noqa: E402

D = lambda s: datetime.strptime(s, "%Y-%m-%d")  # noqa: E731


class TestAccountAsOf(unittest.TestCase):
    """as-of 조회가 '금액'뿐 아니라 '어느 스냅샷인지'까지 돌려주는가."""

    def setUp(self):
        # 실제 운영 값(2026-07-27~08-10): BEAR 현금 대피로 자산이 3주간 완전 불변
        self.eq = {D("2026-07-27"): 2712844,
                   D("2026-08-03"): 2712844,
                   D("2026-08-10"): 2712844}

    def test_returns_snapshot_date_with_value(self):
        t, e = AccountReader.on_or_before(self.eq, D("2026-08-05"))
        self.assertEqual(t, D("2026-08-03"))   # 이하 최근 스냅샷
        self.assertEqual(e, 2712844)

    def test_missing_returns_pair_of_none(self):
        t, e = AccountReader.on_or_before(self.eq, D("2026-07-01"))
        self.assertIsNone(t)
        self.assertIsNone(e)


class TestIntervalScoring(unittest.TestCase):
    """실계좌 구간 채점 — 프로덕션 AccountReader.interval_return를 직접 호출한다.

    (판정 규칙을 테스트에 복제하면 프로덕션이 바뀌어도 통과하는 순환논리가 된다.)
    """

    @staticmethod
    def score(eq, d0, d1):
        r = AccountReader.interval_return(eq, d0, d1)
        return None if r is None else round(r * 100, 2)

    def test_cash_shelter_zero_is_scored_not_dropped(self):
        """핵심 회귀: 금액이 같아도 서로 다른 스냅샷이면 진짜 0%다."""
        eq = {D("2026-08-03"): 2712844, D("2026-08-10"): 2712844}
        self.assertEqual(self.score(eq, D("2026-08-03"), D("2026-08-10")), 0.0)

    def test_stale_snapshot_is_missing(self):
        """구간 양끝이 같은 스냅샷을 가리키면(미갱신) 결측이어야 한다."""
        eq = {D("2026-08-03"): 2712844}
        self.assertIsNone(self.score(eq, D("2026-08-03"), D("2026-08-10")))

    def test_real_return_still_computed(self):
        eq = {D("2026-07-13"): 2857754, D("2026-07-20"): 2716964}
        self.assertEqual(self.score(eq, D("2026-07-13"), D("2026-07-20")), -4.93)

    def test_no_data_is_missing(self):
        self.assertIsNone(self.score({}, D("2026-08-03"), D("2026-08-10")))

    def test_zero_equity_is_missing(self):
        """자산 0원은 수익률 계산 불가(0으로 나눔) → 결측."""
        eq = {D("2026-08-03"): 0, D("2026-08-10"): 2712844}
        self.assertIsNone(self.score(eq, D("2026-08-03"), D("2026-08-10")))


class TestLedgerReporting(unittest.TestCase):
    """채점 구간 0개일 때 누적을 0%로 둔갑시키지 않는가."""

    @staticmethod
    def account_pct(acc_cum, acc_intervals):
        return round((acc_cum - 1) * 100, 2) if acc_intervals else None

    def test_no_scored_interval_reports_none_not_zero(self):
        self.assertIsNone(self.account_pct(1.0, 0))

    def test_scored_zero_return_reports_zero(self):
        self.assertEqual(self.account_pct(1.0, 1), 0.0)


class TestTelegramRendering(unittest.TestCase):
    """알림이 None에서 죽지 않고, 결측을 0%로 표시하지 않는가.
    (프로덕션 render_account를 직접 호출)
    """

    render = staticmethod(render_account)

    def test_none_renders_as_missing(self):
        self.assertIn("데이터 없음", self.render({"account_pct": None}))

    def test_absent_key_renders_as_missing(self):
        """옛 원장(키 자체가 없음)에서도 0%로 둔갑하면 안 된다."""
        self.assertIn("데이터 없음", self.render({}))

    def test_zero_renders_as_zero(self):
        self.assertEqual(self.render({"account_pct": 0.0}), "• 실계좌: +0.00%")

    def test_negative_renders_signed(self):
        self.assertEqual(self.render({"account_pct": -22.49}), "• 실계좌: -22.49%")


class TestLedgerContract(unittest.TestCase):
    """실제 생성된 원장이 새 계약을 지키는가(있을 때만 검사)."""

    def test_current_ledger_shape(self):
        p = ROOT / "data" / "shadow_ledger.json"
        if not p.exists():
            self.skipTest("원장 미생성")
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("account_intervals", d)
        n = d["account_intervals"]
        # 채점 구간이 0개면 누적은 반드시 None이어야 한다
        if n == 0:
            self.assertIsNone(d["account_pct"])
        else:
            self.assertIsNotNone(d["account_pct"])
        # 구간별 account 값의 개수와 account_intervals가 일치해야 한다
        scored = sum(1 for r in d.get("intervals", []) if r.get("account") is not None)
        self.assertEqual(scored, n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
