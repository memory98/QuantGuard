#!/usr/bin/env python3
"""
tests/test_paper_measurement.py — [#OPEN-PB ⓐⓑⓒⓓ] 종이봇 측정 신뢰성 (2026-09-30).

이 봇은 돈이 0이지만 존재 이유가 '전략 판정의 측정기'라, 조용히 틀린 수치가 곧 피해다.
2026-09-30 감사에서 찾은 4건을 회귀로 못 박는다. 구버전은 ⓐⓑⓒⓓ 전부 통과하지 못한다(V2).

  ⓐ 상태파일 비원자적 쓰기 → 쓰는 중 사망 시 계좌 유실(오버나이트 전환이 위험도를 올렸음)
  ⓑ 오버나이트 갭이 어느 날의 일별 수익률에도 안 들어감 → 일별 곱이 자산곡선과 불일치
  ⓒ last_price 신선도 미검증 → 며칠 전 시세로 캐리 포지션 평가(fix41과 동일 클래스)
  ⓓ 일일요약이 '세션'마다 기록 → 같은 날 두 줄·가짜 하루(실측: 2026-07-30 수동종료)
"""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "paper_trader"))
sys.path.insert(0, str(ROOT / "backtest"))
import us_paper_bot as pb  # noqa: E402

CFG = {
    "capital_krw": 10_000_000, "high_window": 20, "ma_window": 50,
    "trail": 0.10, "stop": 0.05, "max_pos": 5, "cost": 0.0005,
    "slippage": 0.001, "whole_shares": True, "grace_min": 30,
    "poll_sec": 180, "universe_size": 40, "candidate_pool": ["AAA"],
    "liquidate_on_close": False,
}


def ser(vals):
    idx = pd.date_range(end=datetime(2026, 9, 30), periods=len(vals), freq="D")
    return pd.Series([float(v) for v in vals], index=idx)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(pb, "STATE", t / "state.json"),
            mock.patch.object(pb, "TRADES", t / "trades.jsonl"),
            mock.patch.object(pb, "DAILY", t / "daily.jsonl"),
            mock.patch.object(pb, "LIVE", t / "live.json"),
            mock.patch.object(pb, "UNIV", t / "univ.json"),
            mock.patch.object(pb, "LOG", t / "bot.log"),
            mock.patch.object(pb, "get_fx", lambda: 1400.0),
            mock.patch.object(pb, "fetch", lambda *a, **k: ser([500, 505])),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def bot_with(self, pos=None, cash=0.0):
        pf = pb.Portfolio(CFG)
        pf.cash = cash
        pf.pos = pos or {}
        with mock.patch.object(pb, "get_universe", return_value=["AAA"]):
            bot = pb.Bot(CFG, pf, mock.Mock())
        return bot, pf


class TestStateDurability(Base):
    """ⓐ — 상태파일은 오버나이트 이후 포지션의 유일한 기록이다."""

    def test_save_keeps_backup_generation(self):
        _, pf = self.bot_with()
        pf.cash = 111.0
        pf.save()
        pf.cash = 222.0
        pf.save()
        bak = pb.STATE.with_suffix(".bak")
        self.assertTrue(bak.exists(), "백업 세대가 없다 — 손상 시 복구 불가")
        self.assertEqual(json.loads(bak.read_text())["cash_usd"], 111.0)
        self.assertEqual(json.loads(pb.STATE.read_text())["cash_usd"], 222.0)

    def test_no_tmp_left_behind(self):
        _, pf = self.bot_with()
        pf.save()
        self.assertFalse(pb.STATE.with_suffix(".tmp").exists(),
                         "tmp 파일이 남았다 — 원자적 교체가 아니다")

    def test_corrupt_state_recovers_from_backup(self):
        _, pf = self.bot_with()
        pf.cash = 777.0
        pf.save()
        pf.cash = 888.0
        pf.save()                                  # .bak = 777
        pb.STATE.write_text("{ 깨진 json", encoding="utf-8")
        d = pb.Portfolio._load_state()
        self.assertEqual(d["cash_usd"], 777.0, "백업에서 복구하지 못했다")

    def test_both_corrupt_refuses_to_start(self):
        """조용히 새 계좌를 만들면 포지션 유실이 '정상 시작'으로 위장된다."""
        _, pf = self.bot_with()
        pf.save()
        pb.STATE.write_text("깨짐", encoding="utf-8")
        pb.STATE.with_suffix(".bak").write_text("깨짐", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            pb.Portfolio._load_state()


class TestGapAccounting(Base):
    """ⓑ — 전일 마감 → 당일 시가 갭이 일별 수익률에 들어가야 한다."""

    def _summary(self, prev_close, open_eq, end_price):
        bot, pf = self.bot_with(pos={"AAA": {"entry": 100.0, "peak": 100.0,
                                             "shares": 10, "entry_at": "x"}})
        pf.last_close_equity_krw = prev_close
        bot.session_traded = True
        bot.open_equity_krw = open_eq
        bot.day_start_krw = prev_close
        bot.last_price = {"AAA": end_price}
        bot.last_seen = {"AAA": time.time()}
        bot.daily_summary(1400.0, "장마감")
        return [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")][-1]

    def test_gap_is_recorded_and_composes(self):
        # 전일마감 1,000,000 / 개장 1,050,000(갭 +5%) / 마감 주가 110 → 평가 110*10*fx0
        rec = self._summary(1_000_000, 1_050_000, 110.0)
        self.assertIsNotNone(rec["gap_pct"], "갭이 기록되지 않았다")
        self.assertAlmostEqual(rec["gap_pct"], 5.0, places=1)
        # 갭 × 장중 = 당일 (복리 결합)
        composed = (1 + rec["gap_pct"] / 100) * (1 + rec["intraday_pct"] / 100) - 1
        self.assertAlmostEqual(composed * 100, rec["day_return_pct"], places=1,
                               msg="갭×장중이 당일 수익률과 맞지 않는다")

    def test_baseline_is_prev_close_not_open(self):
        rec = self._summary(1_000_000, 1_050_000, 110.0)
        self.assertEqual(rec["start_equity_krw"], 1_000_000,
                         "기준이 전일 마감이 아니라 개장 평가액이다 — 갭이 유실된다")

    def test_prev_close_is_persisted_for_next_day(self):
        rec = self._summary(1_000_000, 1_050_000, 110.0)
        saved = json.loads(pb.STATE.read_text(encoding="utf-8"))
        self.assertEqual(round(saved["last_close_equity_krw"]), rec["end_equity_krw"],
                         "다음 날 갭 기준이 저장되지 않았다")


class TestStalePricing(Base):
    """ⓒ — 낡은 시세로 평가했으면 수치를 내되 표기해야 한다(fix41과 동일 원칙)."""

    def _rec(self, age_sec):
        bot, pf = self.bot_with(pos={"AAA": {"entry": 100.0, "peak": 100.0,
                                             "shares": 10, "entry_at": "x"}})
        bot.session_traded = True
        bot.open_equity_krw = 1_000_000
        bot.day_start_krw = 1_000_000
        bot.last_price = {"AAA": 100.0}
        bot.last_seen = {"AAA": time.time() - age_sec}
        bot.daily_summary(1400.0, "장마감")
        return [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")][-1]

    def test_fresh_price_not_flagged(self):
        self.assertIsNone(self._rec(10)["stale_priced"])

    def test_stale_price_is_flagged(self):
        rec = self._rec(4 * 3600)
        self.assertEqual(rec["stale_priced"], ["AAA"],
                         "며칠 전 시세로 평가했는데 조용히 넘어갔다")

    def test_never_priced_holding_is_flagged(self):
        bot, pf = self.bot_with(pos={"ZZZ": {"entry": 50.0, "peak": 50.0,
                                             "shares": 2, "entry_at": "x"}})
        bot.session_traded = True
        bot.open_equity_krw = 1_000
        bot.day_start_krw = 1_000
        bot.daily_summary(1400.0, "장마감")
        rec = [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")][-1]
        self.assertEqual(rec["stale_priced"], ["ZZZ"])


class TestOneRowPerDay(Base):
    """ⓓ — 하루 = 한 줄. 세션마다 줄이 쌓이면 vs SPY 시계열이 오염된다."""

    def _session(self, reason, traded=True, end_price=100.0):
        bot, pf = self.bot_with(pos={"AAA": {"entry": 100.0, "peak": 100.0,
                                             "shares": 10, "entry_at": "x"}})
        bot.session_traded = traded
        bot.open_equity_krw = 1_000_000
        bot.day_start_krw = 1_000_000
        bot.last_price = {"AAA": end_price}
        bot.last_seen = {"AAA": time.time()}
        bot.daily_summary(1400.0, reason)

    def test_two_sessions_same_day_is_one_row(self):
        self._session("수동종료")
        self._session("장마감", end_price=105.0)
        rows = [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")]
        self.assertEqual(len(rows), 1, f"같은 날 레코드가 {len(rows)}줄 쌓였다")
        self.assertEqual(rows[0]["reason"], "장마감", "마지막 세션 결과로 갱신되지 않았다")
        self.assertEqual(rows[0]["sessions"], 2, "세션 횟수가 기록되지 않았다")

    def test_session_without_market_cycle_writes_nothing(self):
        """장 열기 전 잠깐 켰다 끈 경우 — 가짜 '하루'를 만들면 안 된다."""
        self._session("수동종료", traded=False)
        self.assertFalse(pb.DAILY.exists() and pb.DAILY.read_text().strip(),
                         "장중 순찰이 없었는데 일일요약이 기록됐다(가짜 하루)")

    def test_regime_is_tagged(self):
        self._session("장마감")
        rec = [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")][0]
        self.assertEqual(rec["regime"], "overnight",
                         "레짐 표기가 없으면 데이트레이드 시절 데이터와 섞인다")


if __name__ == "__main__":
    unittest.main()


def _open_once():
    """첫 호출만 '장 열림'. market_open은 write_live에서도 불리므로 횟수를 가정하지 않는다."""
    state = {"n": 0}

    def fn(*a, **k):
        state["n"] += 1
        return state["n"] == 1
    return fn


class TestRestartKeepsDayBaseline(Base):
    """[ⓑ-2] 장중에 껐다 켜도 당일 수익률 기준점이 유지돼야 한다.

    기준점을 재시작 시각으로 다시 잡으면 껐다 켜기 전 구간이 어느 날에도 계상되지 않는다
    (#OPEN-PB ⓑ와 같은 클래스). 이 봇은 수동 재시작이 잦아 매번 구멍이 난다.
    """

    def test_same_day_restart_inherits_baseline(self):
        bot, pf = self.bot_with()
        pf.day_start_date = pb.trading_date()
        pf.day_start_krw = 10_000_000
        pf.day_open_krw = 10_050_000
        bot.need_day_start = True
        with mock.patch.object(bot, "cycle", return_value={}), \
             mock.patch.object(pb, "market_open", _open_once()), \
             mock.patch.object(pb.subprocess, "Popen"), \
             mock.patch.object(pb.time, "sleep", lambda *_: None):
            bot.run()
        self.assertEqual(bot.day_start_krw, 10_000_000,
                         "재시작에서 기준점이 새로 잡혔다(구간 유실)")
        self.assertEqual(bot.open_equity_krw, 10_050_000)

    def test_new_day_sets_fresh_baseline(self):
        bot, pf = self.bot_with()
        pf.day_start_date = "2026-01-01"          # 다른 거래일
        pf.day_start_krw = 777
        pf.last_close_equity_krw = 9_900_000
        bot.need_day_start = True
        with mock.patch.object(bot, "cycle", return_value={}), \
             mock.patch.object(pb, "market_open", _open_once()), \
             mock.patch.object(pb.subprocess, "Popen"), \
             mock.patch.object(pb.time, "sleep", lambda *_: None):
            bot.run()
        self.assertEqual(bot.day_start_krw, 9_900_000,
                         "새 거래일인데 전일 마감 기준을 쓰지 않았다")
        self.assertEqual(pf.day_start_date, pb.trading_date())
