#!/usr/bin/env python3
"""
tests/test_paper_selection.py — 종이봇 종목선택 3건 (2026-10-01).

① 돌파 판정의 자기참조 제거 — `tail(20).max()` 안에 오늘 값이 들어 있어 실질 '직전 19일'
   돌파였고, 장중가 vs 일간종가 비교라 **개장 직후엔 "시가 ≥ 직전 19일 최고치"**,
   즉 갭 상승 추격 매수로 작동했다(2026-09-30 실측: 개장 2초 만에 5슬롯 만석).
② 재진입 쿨다운 — 진입 필터가 '보유중이 아님'만 봐서 손절 직후 같은 종목을 다시 살 수 있었다.
③ 조용한 탈락 관측 — 스크리닝 실패를 세지 않고 전멸 시 후보풀 앞 40종으로 무경고 폴백.

검증 강도 V2(변이 구별): 구버전은 ①②③ 모두 통과하지 못한다.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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
    "liquidate_on_close": False, "reentry_cooldown_days": 1,
}


def ser(vals):
    idx = pd.date_range(end=datetime(2026, 10, 1), periods=len(vals), freq="D")
    return pd.Series([float(v) for v in vals], index=idx)


class FakeFeed:
    def __init__(self, series_map):
        self.series_map = series_map

    def snapshot(self, syms, ma_window):
        return {s: self.series_map[s] for s in syms if s in self.series_map}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        for name in ("STATE", "TRADES", "DAILY", "LIVE", "UNIV", "LOG"):
            p = mock.patch.object(pb, name, t / f"{name.lower()}.json")
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(pb, "get_fx", lambda: 1400.0)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_bot(self, universe, feed):
        pf = pb.Portfolio(CFG)
        pf.cash = 100000.0
        pf.pos = {}
        with mock.patch.object(pb, "get_universe", return_value=universe):
            return pb.Bot(CFG, pf, feed), pf


class TestBreakoutSelfReference(Base):
    """① 오늘 바는 비교 대상에서 빠져야 한다."""

    def test_today_spike_alone_is_not_breakout(self):
        """과거 19일이 100인데 오늘만 120으로 튄 경우: 구버전은 max에 120이 포함돼
        `120 >= 120`으로 **돌파 성립**. 신버전은 과거 최고 100과 비교해 성립(정상)."""
        bot, _ = self.make_bot(["AAA"], FakeFeed({}))
        flat = ser([100.0] * 60 + [120.0])
        self.assertTrue(bot._breakout(flat), "진짜 신고가인데 놓쳤다")

    def test_below_past_high_is_not_breakout(self):
        """핵심 — 과거에 더 높은 종가가 있으면 오늘 값이 그보다 낮을 때 돌파가 아니다.
        구버전은 오늘 값이 max에 포함돼 자기 자신과 비교하는 경로가 생긴다."""
        bot, _ = self.make_bot(["AAA"], FakeFeed({}))
        # 과거 20일 중 최고 150, 오늘 130 → 돌파 아님
        s = ser([100.0] * 45 + [150.0] + [100.0] * 14 + [130.0])
        self.assertFalse(bot._breakout(s), "과거 최고가 아래인데 돌파로 판정했다")

    def test_equal_to_past_high_is_breakout(self):
        bot, _ = self.make_bot(["AAA"], FakeFeed({}))
        s = ser([100.0] * 59 + [110.0] + [110.0])
        self.assertTrue(bot._breakout(s))

    def test_insufficient_history_is_not_breakout(self):
        """히스토리가 창보다 짧으면 판정하지 않는다(과거 구간을 뺐으므로 경계 확인)."""
        bot, _ = self.make_bot(["AAA"], FakeFeed({}))
        self.assertFalse(bot._breakout(ser([100.0] * 30 + [200.0])))


class TestReentryCooldown(Base):
    """② 손절 직후 같은 종목을 다시 사지 않는다."""

    def _breakout_series(self):
        return ser([100.0] * 60 + [130.0])

    def test_recently_exited_symbol_is_skipped(self):
        feed = FakeFeed({"AAA": self._breakout_series()})
        bot, pf = self.make_bot(["AAA"], feed)
        pf.last_exit["AAA"] = pb.trading_date()      # 오늘 청산했다
        bot.cycle(1400.0, allow_entry=True)
        self.assertNotIn("AAA", pf.pos, "쿨다운 중인데 재진입했다(핑퐁)")

    def test_old_exit_does_not_block(self):
        feed = FakeFeed({"AAA": self._breakout_series()})
        bot, pf = self.make_bot(["AAA"], feed)
        old = (datetime.strptime(pb.trading_date(), "%Y-%m-%d") - timedelta(days=5))
        pf.last_exit["AAA"] = old.strftime("%Y-%m-%d")
        bot.cycle(1400.0, allow_entry=True)
        self.assertIn("AAA", pf.pos, "쿨다운이 지났는데도 막혔다")

    def test_sell_records_exit_date(self):
        feed = FakeFeed({"AAA": self._breakout_series()})
        bot, pf = self.make_bot(["AAA"], feed)
        pf.pos = {"AAA": {"entry": 100.0, "peak": 100.0, "shares": 1, "entry_at": "x"}}
        pf.sell("AAA", 90.0, "손절 -5%", 1400.0)
        self.assertEqual(pf.last_exit.get("AAA"), pb.trading_date())

    def test_cooldown_persists_across_restart(self):
        """봇을 껐다 켜도 쿨다운이 유지돼야 한다(state에 저장)."""
        feed = FakeFeed({"AAA": self._breakout_series()})
        _, pf = self.make_bot(["AAA"], feed)
        pf.last_exit["AAA"] = pb.trading_date()
        pf.save()
        pf2 = pb.Portfolio(CFG)
        self.assertEqual(pf2.last_exit.get("AAA"), pb.trading_date())


class TestScreeningObservability(Base):
    """③ 조용한 탈락 금지."""

    def test_screen_reports_success_count(self):
        def fake_dv(sym, days=20):
            if sym in ("BAD1", "BAD2"):
                raise RuntimeError("조회 실패")
            return 1000.0
        with mock.patch.object(pb, "avg_dollar_volume", fake_dv):
            uni, ok_n = pb.screen_universe(["A", "B", "BAD1", "BAD2"], 10)
        self.assertEqual(ok_n, 2, "성공 건수를 반환하지 않는다")
        self.assertEqual(sorted(uni), ["A", "B"])

    def test_low_coverage_is_logged(self):
        def fake_dv(sym, days=20):
            if sym != "A":
                raise RuntimeError("실패")
            return 1000.0
        cfg = dict(CFG, candidate_pool=["A", "B", "C", "D"], universe_size=4)
        with mock.patch.object(pb, "avg_dollar_volume", fake_dv), \
             mock.patch.object(pb, "log") as m_log:
            pb.get_universe(cfg)
        msgs = " ".join(str(c[0][0]) for c in m_log.call_args_list)
        self.assertIn("커버리지 부족", msgs, "커버리지 미달이 조용히 넘어갔다")

    def test_total_failure_fallback_is_loud(self):
        def fake_dv(sym, days=20):
            raise RuntimeError("전멸")
        cfg = dict(CFG, candidate_pool=["A", "B"], universe_size=2)
        with mock.patch.object(pb, "avg_dollar_volume", fake_dv), \
             mock.patch.object(pb, "log") as m_log:
            uni = pb.get_universe(cfg)
        msgs = " ".join(str(c[0][0]) for c in m_log.call_args_list)
        self.assertEqual(uni, ["A", "B"])
        self.assertIn("전멸", msgs, "무경고 폴백이 그대로다")

    def test_universe_price_dropout_is_logged(self):
        """유니버스 종목이 시세 수집에서 조용히 빠지는 것도 드러나야 한다."""
        feed = FakeFeed({"AAA": ser([100.0] * 61)})     # BBB는 누락
        bot, _ = self.make_bot(["AAA", "BBB"], feed)
        with mock.patch.object(pb, "log") as m_log:
            bot.cycle(1400.0, allow_entry=False)
        msgs = " ".join(str(c[0][0]) for c in m_log.call_args_list)
        self.assertIn("유니버스 시세 누락", msgs)
        self.assertIn("BBB", msgs)


if __name__ == "__main__":
    unittest.main()
