#!/usr/bin/env python3
"""tests/test_signal_quality.py — 신호 품질 관측 (#OPEN-S/#OPEN-B, fix32).

검증 핵심:
  - STEP B 판정이 **표본 부족 시 절대 결론을 내지 않는가**(과최적화 방지의 마지막 방벽)
  - 발동 기준이 사전 고정값대로 동작하는가(연속 4주 / 스프레드 누적)
  - IC 계산이 순위상관으로 올바른가(완전 정/역상관 경계)
  - 계산 불가를 0으로 둔갑시키지 않는가(fix31과 같은 조용한 실패 클래스)
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("backtest", "strategies", "rambdaA", "scripts"):
    sys.path.insert(0, str(ROOT / p))
from signal_quality import (  # noqa: E402
    SignalQualityAnalyzer, SignalQualityLedger,
    MIN_SAMPLE_WEEKS, IC_NEGATIVE_STREAK, PROXY_CORR_FLOOR,
)
from notify_telegram_shadow import render_signal_quality  # noqa: E402


class FakePrices:
    """PriceProvider 대역 — {code: {date: price}}."""

    def __init__(self, table):
        self.table = table

    def at(self, code, date):
        return self.table.get(code, {}).get(date)


def snap(date, items):
    """items: [(code, momentum)]"""
    return {"date": date,
            "universe": [{"code": c, "momentum": m} for c, m in items],
            "market": {"market_status": "BULL"}}


class TestICComputation(unittest.TestCase):
    """IC가 순위상관으로 올바르게 나오는가."""

    D0, D1 = datetime(2026, 8, 3), datetime(2026, 8, 10)

    def _run(self, pairs):
        """pairs: [(momentum, 수익률)] → IC"""
        items = [(f"{i:06d}", m) for i, (m, _) in enumerate(pairs)]
        table = {f"{i:06d}": {self.D0: 100.0, self.D1: 100.0 * (1 + r)}
                 for i, (_, r) in enumerate(pairs)}
        a = SignalQualityAnalyzer(FakePrices(table))
        return a.evaluate(snap(self.D0, items), self.D1)

    def test_perfect_positive_ic(self):
        """모멘텀 순위 = 수익 순위면 IC=+1."""
        pairs = [(1.0 - i * 0.01, 0.10 - i * 0.001) for i in range(30)]
        self.assertAlmostEqual(self._run(pairs)["ic"], 1.0, places=3)

    def test_perfect_inverse_ic(self):
        """완전 역전이면 IC=-1 (2026-08-03 주가 이 방향이었다)."""
        pairs = [(1.0 - i * 0.01, -0.10 + i * 0.001) for i in range(30)]
        self.assertAlmostEqual(self._run(pairs)["ic"], -1.0, places=3)

    def test_spread_is_top10_minus_universe(self):
        pairs = [(1.0 - i * 0.01, 0.10 if i < 10 else 0.0) for i in range(30)]
        r = self._run(pairs)
        # top10 +10%, 유니버스 평균 = 10*0.10/30 = 3.333%
        self.assertAlmostEqual(r["top10_pct"], 10.0, places=2)
        self.assertAlmostEqual(r["spread_pct"], 10.0 - (10 * 10 / 30), places=2)

    def test_thin_sample_returns_none_not_zero(self):
        """표본이 얇으면 None — 0으로 둔갑 금지(fix31류 조용한 실패 방지)."""
        self.assertIsNone(self._run([(0.5, 0.01), (0.3, 0.02)]))

    def test_missing_prices_return_none(self):
        items = [(f"{i:06d}", 0.5) for i in range(30)]
        a = SignalQualityAnalyzer(FakePrices({}))     # 가격 전무
        self.assertIsNone(a.evaluate(snap(self.D0, items), self.D1))


class TestVerdictGuards(unittest.TestCase):
    """STEP B — 표본 부족 시 결론 금지가 핵심."""

    def _ledger(self, ics, spreads=None):
        led = SignalQualityLedger(Path(tempfile.mkdtemp()) / "l.json")
        spreads = spreads or [1.0] * len(ics)
        for i, (ic, sp) in enumerate(zip(ics, spreads)):
            led.upsert({"from": f"w{i}", "to": f"w{i+1}", "ic": ic,
                        "spread_pct": sp, "market_status": "BULL"})
        return led

    def test_insufficient_below_min_sample(self):
        """25주까지는 아무리 나빠도 판정하지 않는다."""
        led = self._ledger([-0.9] * (MIN_SAMPLE_WEEKS - 1), [-5.0] * (MIN_SAMPLE_WEEKS - 1))
        v = led.verdict()
        self.assertEqual(v["status"], "INSUFFICIENT")
        self.assertEqual(v["need"], 1)

    def test_ok_when_healthy(self):
        led = self._ledger([0.2] * MIN_SAMPLE_WEEKS)
        self.assertEqual(led.verdict()["status"], "OK")

    def test_degraded_on_negative_spread_cum(self):
        led = self._ledger([0.2] * MIN_SAMPLE_WEEKS, [-1.0] * MIN_SAMPLE_WEEKS)
        self.assertEqual(led.verdict()["status"], "DEGRADED")

    def test_degraded_on_ic_ma_streak(self):
        """IC 이동평균이 연속 음수여야 발동 — 한두 주 음수로는 안 됨."""
        led = self._ledger([-0.5] * (MIN_SAMPLE_WEEKS + IC_NEGATIVE_STREAK))
        v = led.verdict()
        self.assertEqual(v["status"], "DEGRADED")
        self.assertGreaterEqual(v["ic_ma_negative_streak"], IC_NEGATIVE_STREAK)

    def test_single_bad_week_does_not_trigger(self):
        """노이즈 추종 방지: 한 주 폭락해도 기준 미발동."""
        ics = [0.3] * (MIN_SAMPLE_WEEKS - 1) + [-0.9]
        led = self._ledger(ics, [1.0] * MIN_SAMPLE_WEEKS)
        self.assertEqual(led.verdict()["status"], "OK")

    def test_none_ic_rows_excluded_from_sample(self):
        """계산 불가 주는 표본에 안 들어간다(가짜 누적 방지)."""
        led = self._ledger([0.1] * 3)
        led.upsert({"from": "x", "to": "y", "ic": None,
                    "spread_pct": None, "market_status": "BEAR"})
        self.assertEqual(led.verdict()["weeks"], 3)


class TestAlerts(unittest.TestCase):
    def _led(self, **last):
        led = SignalQualityLedger(Path(tempfile.mkdtemp()) / "l.json")
        rec = {"from": "a", "to": "b", "ic": 0.1, "spread_pct": 1.0,
               "market_status": "BULL", "proxy_corr": 0.95}
        rec.update(last)
        led.upsert(rec)
        return led

    def test_no_alert_when_healthy(self):
        """정상이면 침묵 — 경보 피로 방지."""
        self.assertEqual(self._led().alerts(), [])

    def test_proxy_corr_alert_below_floor(self):
        a = self._led(proxy_corr=PROXY_CORR_FLOOR - 0.01).alerts()
        self.assertTrue(any("대리지표" in x for x in a))

    def test_no_proxy_alert_when_none(self):
        """상관 계산 불가를 '저하'로 오인하면 안 된다."""
        self.assertEqual(self._led(proxy_corr=None).alerts(), [])


class TestUpsert(unittest.TestCase):
    def test_rerun_updates_not_duplicates(self):
        """같은 주 재실행이 표본을 부풀리면 안 된다."""
        led = SignalQualityLedger(Path(tempfile.mkdtemp()) / "l.json")
        for ic in (0.1, 0.9):
            led.upsert({"from": "2026-08-03", "to": "2026-08-10",
                        "ic": ic, "spread_pct": 1.0, "market_status": "BULL"})
        self.assertEqual(len(led.data["records"]), 1)
        self.assertEqual(led.data["records"][0]["ic"], 0.9)


class TestTelegramRendering(unittest.TestCase):
    def test_empty_ledger_renders_nothing(self):
        self.assertEqual(render_signal_quality(None), [])
        self.assertEqual(render_signal_quality({"records": []}), [])

    def test_insufficient_shows_progress_not_conclusion(self):
        out = "\n".join(render_signal_quality({
            "records": [{"ic": -0.76, "spread_pct": -5.49, "market_status": "BEAR",
                         "proxy_corr": 0.97}],
            "verdict": {"status": "INSUFFICIENT", "weeks": 1, "need": 25},
            "criteria": {"min_sample_weeks": 26}}))
        self.assertIn("보류", out)
        self.assertIn("BEAR라 미매수", out)     # 오해 방지 문구
        self.assertNotIn("DEGRADED", out)

    def test_uncomputable_week_not_shown_as_zero(self):
        out = "\n".join(render_signal_quality({
            "records": [{"ic": None, "spread_pct": None, "market_status": "BULL"}],
            "verdict": {"status": "INSUFFICIENT", "weeks": 0, "need": 26}}))
        self.assertIn("계산 불가", out)
        self.assertNotIn("+0.00", out)


class TestLedgerContract(unittest.TestCase):
    def test_real_ledger_shape(self):
        p = ROOT / "signal_quality_ledger.json"
        if not p.exists():
            self.skipTest("원장 미생성")
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("criteria", d)
        # 발동 기준은 사전 고정값과 일치해야 한다(사후 변경 감지)
        self.assertEqual(d["criteria"]["min_sample_weeks"], MIN_SAMPLE_WEEKS)
        self.assertEqual(d["criteria"]["ic_negative_streak"], IC_NEGATIVE_STREAK)
        for r in d["records"]:
            self.assertIn("market_status", r)   # BEAR 해석에 필수


if __name__ == "__main__":
    unittest.main(verbosity=2)
