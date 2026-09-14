#!/usr/bin/env python3
"""tests/test_live_gate.py — 실계좌 계속/중단 게이트 (AUDIT.md ③ STEP E, fix39).

검증 핵심(이 게이트가 틀리는 두 방향을 모두 본다):
  - **오작동(거짓 중단)**: 정상 운영(BEAR 현금 주, 밴드 스킵, 테스트 모드 기록,
    창 이전의 과거 사고)을 중단 사유로 오인하지 않는가. 이쪽이 더 위험하다 —
    멀쩡한 검증을 중간에 끊으면 26주 표본이 영영 안 모인다.
  - **미탐(놓친 중단)**: 실제 사고(주문 실패, 반쪽 리밸런싱, 잔고 결측,
    대리지표 붕괴)를 놓치지 않는가.
  - **조기 판정 금지**: 13주 미만에서 스프레드 기준이, 26주 미만에서 지평 판정이
    절대 발동하지 않는가(STEP B와 같은 과최적화 방벽).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("backtest", "strategies", "rambdaA", "scripts"):
    sys.path.insert(0, str(ROOT / p))
from live_gate import (  # noqa: E402
    ExecutionIntegrityCheck, LiveTradingGate,
    HORIZON_WEEKS, MIDPOINT_WEEKS, MIDPOINT_SPREAD_FLOOR_PCT,
    PROXY_CORR_FLOOR, PROXY_BREACH_STREAK,
)
from notify_telegram_shadow import render_live_gate  # noqa: E402


def rec(frm, to, ic=0.1, spread=0.5, corr=0.95):
    return {"from": frm, "to": to, "ic": ic, "spread_pct": spread,
            "proxy_corr": corr, "market_status": "BULL"}


def ledger(records):
    return {"records": records}


def weeks(n, spread=0.5, corr=0.95):
    """n주치 연속 구간. 날짜는 2026-08-03부터 7일 간격."""
    from datetime import date, timedelta
    d0 = date(2026, 8, 3)
    out = []
    for i in range(n):
        a = d0 + timedelta(weeks=i)
        b = d0 + timedelta(weeks=i + 1)
        out.append(rec(a.isoformat(), b.isoformat(), spread=spread, corr=corr))
    return out


class NoViolations(ExecutionIntegrityCheck):
    def scan(self, since):
        return []


class ArchiveFixture:
    """임시 아카이브 디렉터리에 스냅샷 JSON을 깔아주는 헬퍼."""

    def __init__(self, files: dict):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for day, payload in files.items():
            (self.dir / f"{day}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def checker(self):
        return ExecutionIntegrityCheck(self.dir)


def snapshot(*, equity=2_700_000, executed=None, unsettled=False,
             test_mode=False, band=None, audit=None):
    korea = {
        "executed_orders": executed if executed is not None else [],
        "buys_skipped_unsettled": unsettled,
        "skipped_band": band if band is not None else [],
        "sell_settled": True,
    }
    if audit is not None:
        korea["execution_audit"] = audit
    return {"total_equity_checked": equity, "force_test_mode": test_mode,
            "korea": korea}


# ─────────────────────────── 실행 무결성 ───────────────────────────

class TestExecutionIntegrity(unittest.TestCase):

    def test_정상_주간은_위반_없음(self):
        fx = ArchiveFixture({"2026-08-24": snapshot(
            executed=[{"side": "BUY", "code": "363580", "ok": True}])})
        self.assertEqual(fx.checker().scan("2026-08-03"), [])

    def test_BEAR_현금주_주문0건은_위반_아님(self):
        """대피 주는 주문이 0건인 게 정상. 이걸 사고로 세면 게이트가 매주 오발동한다."""
        fx = ArchiveFixture({"2026-08-10": snapshot(executed=[])})
        self.assertEqual(fx.checker().scan("2026-08-03"), [])

    def test_밴드_스킵은_위반_아님(self):
        """skipped_band는 설계된 스킵(밴드 내 유지)이지 집행 실패가 아니다."""
        fx = ArchiveFixture({"2026-08-24": snapshot(band=[["102780", "밴드내"]])})
        self.assertEqual(fx.checker().scan("2026-08-03"), [])

    def test_주문_실패는_위반(self):
        fx = ArchiveFixture({"2026-08-24": snapshot(executed=[
            {"side": "BUY", "code": "363580", "ok": True},
            {"side": "BUY", "code": "395270", "ok": False},
        ])})
        v = fx.checker().scan("2026-08-03")
        self.assertEqual([x["kind"] for x in v], ["ORDER_FAILED"])

    def test_반쪽_리밸런싱은_위반(self):
        """2026-07-13 실사고 클래스 — 매도 미결제로 매수를 건너뛴 경우."""
        fx = ArchiveFixture({"2026-08-24": snapshot(unsettled=True)})
        self.assertEqual([x["kind"] for x in fx.checker().scan("2026-08-03")],
                         ["PARTIAL_REBALANCE"])

    def test_잔고_결측은_위반(self):
        """2026-06-30 폴백 오염 클래스 — 총자산이 0/결측이면 그 주 표본 전체가 거짓."""
        for bad in (0, None, "3,500,000"):
            with self.subTest(equity=bad):
                fx = ArchiveFixture({"2026-08-24": snapshot(equity=bad)})
                self.assertEqual([x["kind"] for x in fx.checker().scan("2026-08-03")],
                                 ["EQUITY_MISSING"])

    def test_체결감사_불일치는_위반이지만_DISABLED는_아님(self):
        off = ArchiveFixture({"2026-08-24": snapshot(
            audit={"ok": True, "reason": "DISABLED", "orders": []})})
        self.assertEqual(off.checker().scan("2026-08-03"), [])
        bad = ArchiveFixture({"2026-08-24": snapshot(
            audit={"ok": False, "reason": "PARTIAL"})})
        self.assertEqual([x["kind"] for x in bad.checker().scan("2026-08-03")],
                         ["EXEC_AUDIT_MISMATCH"])

    def test_테스트모드_기록은_무시(self):
        """콘솔 테스트(force_test_mode)는 실전 표본이 아니다 — 세면 오발동."""
        fx = ArchiveFixture({"2026-08-24": snapshot(test_mode=True, unsettled=True)})
        self.assertEqual(fx.checker().scan("2026-08-03"), [])

    def test_창_이전_사고는_무시(self):
        """07-13 반쪽 리밸런싱은 관측 창(08-03) 밖 — 과거 사고로 새 검증을 죽이지 않는다."""
        fx = ArchiveFixture({"2026-07-13": snapshot(unsettled=True),
                             "2026-08-24": snapshot()})
        self.assertEqual(fx.checker().scan("2026-08-03"), [])
        # 창을 넓히면 같은 파일이 잡힌다 — 기준이 헛돌지 않음을 확인
        self.assertEqual([x["kind"] for x in fx.checker().scan("2026-07-01")],
                         ["PARTIAL_REBALANCE"])

    def test_아카이브_부재는_위반_아님(self):
        """없는 파일을 근거로 실계좌를 멈추는 게 더 위험한 오작동이다."""
        c = ExecutionIntegrityCheck(Path("/nonexistent/dir/xyz"))
        self.assertEqual(c.scan("2026-08-03"), [])


# ─────────────────────────── 판정 ───────────────────────────

class TestVerdict(unittest.TestCase):

    def gate(self, records, checker=None):
        return LiveTradingGate(ledger(records), checker or NoViolations())

    def test_정상이면_RUNNING(self):
        v = self.gate(weeks(4)).verdict()
        self.assertEqual(v["status"], "RUNNING")
        self.assertEqual(v["weeks"], 4)
        self.assertEqual(v["need"], HORIZON_WEEKS - 4)

    def test_실행위반이_최우선(self):
        class Bad(ExecutionIntegrityCheck):
            def scan(self, since):
                return [{"date": "2026-08-24", "kind": "ORDER_FAILED", "detail": "x"}]
        # 대리지표까지 동시에 깨져 있어도 실행 위반이 먼저 보고된다
        v = self.gate(weeks(4, corr=0.1), checker=Bad()).verdict()
        self.assertEqual(v["status"], "STOP_EXECUTION")

    def test_대리지표_1주_위반으로는_중단하지_않음(self):
        recs = weeks(3) + [rec("2026-08-24", "2026-08-31", corr=0.5)]
        self.assertEqual(self.gate(recs).verdict()["status"], "RUNNING")

    def test_대리지표_2주_연속이면_STOP_PROXY(self):
        recs = weeks(2) + [rec("2026-08-17", "2026-08-24", corr=0.5),
                           rec("2026-08-24", "2026-08-31", corr=0.4)]
        v = self.gate(recs).verdict()
        self.assertEqual(v["status"], "STOP_PROXY")
        self.assertEqual(v["proxy"]["breach_streak"], PROXY_BREACH_STREAK)

    def test_대리지표_결측은_연속을_끊는다(self):
        """결측을 위반으로 이어붙이면 데이터 구멍이 중단 사유로 둔갑한다."""
        recs = [rec("2026-08-03", "2026-08-10", corr=0.5),
                {"from": "2026-08-10", "to": "2026-08-17", "ic": 0.1,
                 "spread_pct": 0.5, "proxy_corr": None},
                rec("2026-08-17", "2026-08-24", corr=0.5)]
        v = self.gate(recs).verdict()
        self.assertEqual(v["proxy"]["breach_streak"], 1)
        self.assertEqual(v["status"], "RUNNING")

    def test_경계값_상관이_바닥과_같으면_위반_아님(self):
        recs = [rec("2026-08-03", "2026-08-10", corr=PROXY_CORR_FLOOR),
                rec("2026-08-10", "2026-08-17", corr=PROXY_CORR_FLOOR)]
        self.assertEqual(self.gate(recs).verdict()["proxy"]["breach_streak"], 0)

    def test_13주_미만이면_스프레드가_바닥_아래여도_판정하지_않음(self):
        """조기 판정 금지 — 표본 부족 구간에서 중단선이 켜지면 노이즈 추종이다."""
        recs = weeks(MIDPOINT_WEEKS - 1, spread=-5.0)   # 누적 -60%p
        v = self.gate(recs).verdict()
        self.assertEqual(v["status"], "RUNNING")
        self.assertFalse(v["midpoint"]["reached"])
        self.assertLess(v["midpoint"]["spread_cum_pct"], MIDPOINT_SPREAD_FLOOR_PCT)

    def test_13주_도달_후_스프레드_붕괴면_STOP_SIGNAL_EARLY(self):
        recs = weeks(MIDPOINT_WEEKS, spread=-1.0)       # 누적 -13%p
        self.assertEqual(self.gate(recs).verdict()["status"], "STOP_SIGNAL_EARLY")

    def test_13주_도달해도_바닥_위면_계속(self):
        recs = weeks(MIDPOINT_WEEKS, spread=-0.5)       # 누적 -6.5%p
        self.assertEqual(self.gate(recs).verdict()["status"], "RUNNING")

    def test_26주_도달하면_HORIZON_REACHED(self):
        v = self.gate(weeks(HORIZON_WEEKS)).verdict()
        self.assertEqual(v["status"], "HORIZON_REACHED")
        self.assertEqual(v["need"], 0)

    def test_25주까지는_지평_판정_안함(self):
        self.assertEqual(self.gate(weeks(HORIZON_WEEKS - 1)).verdict()["status"],
                         "RUNNING")

    def test_IC결측_구간은_주차로_세지_않음(self):
        """계산 불가를 표본으로 세면 26주가 가짜로 빨리 찬다(fix31 결측 클래스)."""
        recs = weeks(3) + [{"from": "2026-08-24", "to": "2026-08-31",
                            "ic": None, "spread_pct": None, "proxy_corr": 0.95}]
        self.assertEqual(self.gate(recs).verdict()["weeks"], 3)

    def test_빈_원장은_안전하게_RUNNING(self):
        v = self.gate([]).verdict()
        self.assertEqual(v["status"], "RUNNING")
        self.assertIsNone(v["window_start"])
        self.assertIsNone(v["eta"])

    def test_창시작은_원장_첫구간(self):
        self.assertEqual(self.gate(weeks(4)).window_start(), "2026-08-03")

    def test_eta는_남은_주만큼_뒤(self):
        v = self.gate(weeks(4)).verdict()
        self.assertEqual(v["eta"], "2027-02-01")

    def test_기준은_사전고정값_그대로_기록된다(self):
        c = self.gate(weeks(2)).verdict()["criteria"]
        self.assertEqual(c["horizon_weeks"], 26)
        self.assertEqual(c["midpoint_weeks"], 13)
        self.assertEqual(c["midpoint_spread_floor_pct"], -10.0)
        self.assertEqual(c["proxy_corr_floor"], 0.80)
        self.assertEqual(c["proxy_breach_streak"], 2)

    def test_정상이면_경보_침묵(self):
        self.assertEqual(self.gate(weeks(4)).alerts(), [])

    def test_중단시에만_경보(self):
        recs = weeks(2) + [rec("2026-08-17", "2026-08-24", corr=0.5),
                           rec("2026-08-24", "2026-08-31", corr=0.4)]
        self.assertEqual(len(self.gate(recs).alerts()), 1)


# ─────────────────────────── 텔레그램 렌더 ───────────────────────────

class TestRender(unittest.TestCase):

    def test_live_gate_없으면_섹션_자체가_없다(self):
        self.assertEqual(render_live_gate({}), [])
        self.assertEqual(render_live_gate(None), [])

    def test_진행중_렌더(self):
        g = LiveTradingGate(ledger(weeks(4)), NoViolations()).verdict()
        out = "\n".join(render_live_gate({"live_gate": g}))
        self.assertIn("4/26주", out)
        self.assertIn("정상", out)
        self.assertIn("계속", out)
        self.assertIn("2027-02-01", out)

    def test_상관_결측이어도_렌더가_죽지_않는다(self):
        recs = [{"from": "2026-08-03", "to": "2026-08-10", "ic": 0.1,
                 "spread_pct": 0.5, "proxy_corr": None}]
        g = LiveTradingGate(ledger(recs), NoViolations()).verdict()
        out = "\n".join(render_live_gate({"live_gate": g}))
        self.assertIn("측정 불가", out)

    def test_중단_렌더에_사유가_보인다(self):
        recs = weeks(2) + [rec("2026-08-17", "2026-08-24", corr=0.5),
                           rec("2026-08-24", "2026-08-31", corr=0.4)]
        g = LiveTradingGate(ledger(recs), NoViolations()).verdict()
        out = "\n".join(render_live_gate({"live_gate": g}))
        self.assertIn("🛑", out)
        self.assertIn("STOP_PROXY", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
