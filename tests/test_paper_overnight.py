#!/usr/bin/env python3
"""
tests/test_paper_overnight.py — 종이 봇 오버나이트 전환 + 셀렉션 감사 회귀 (2026-09-30).

배경: 전략 정의는 스윙인데 구현이 매일 마감 전량청산(데이트레이드)이라 트레일(-10%)·손절(-5%)이
한 번도 발동한 적이 없었다(실측 거래 7건 전부 '종료-*'). 오버나이트로 바꾸면서 함께 드러난
**치명 버그**를 회귀 테스트로 못 박는다:

  보유 종목이 유니버스에서 밀려나면 `cycle()`이 시세를 안 긁어 청산 루프가 `continue`로
  건너뛰고 **손절·트레일이 조용히 영구 미발동**한다. 당일청산 시절엔 몇 시간이면 끝나
  잘 안 드러났지만, 며칠 들고 가면 손실이 무한정 방치된다.

검증 강도 V2(변이 구별): 구버전은 test_stop_fires_for_holding_outside_universe 를 통과하지 못한다.
⚠️ 모든 파일 경로를 tmp로 갈아끼운다 — 실제 봇이 돌고 있을 수 있어 state/log를 건드리면 안 된다.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "paper_trader"))
sys.path.insert(0, str(ROOT / "backtest"))
import us_paper_bot as pb  # noqa: E402

CFG = {
    "capital_krw": 10_000_000, "high_window": 20, "ma_window": 50,
    "trail": 0.10, "stop": 0.05, "max_pos": 5, "cost": 0.0005,
    "slippage": 0.001, "whole_shares": True, "grace_min": 30,
    "poll_sec": 2, "universe_size": 40, "candidate_pool": ["AAA", "BBB"],
    "liquidate_on_close": False,
}


def ser(vals):
    idx = pd.date_range(end=datetime(2026, 9, 30), periods=len(vals), freq="D")
    return pd.Series([float(v) for v in vals], index=idx)


class FakeFeed:
    """요청받은 심볼만 돌려준다(실제 야후처럼). 요청 목록을 기록해 검증에 쓴다."""

    def __init__(self, prices, on_snapshot=None):
        self.prices = prices          # sym -> 마지막 종가
        self.asked = []
        self.on_snapshot = on_snapshot

    def snapshot(self, syms, ma_window):
        self.asked.append(list(syms))
        if self.on_snapshot:
            self.on_snapshot()
        out = {}
        for s in syms:
            if s in self.prices:
                base = self.prices[s]
                out[s] = ser([base * 0.9] * 60 + [base])
        return out


class PaperBotCase(unittest.TestCase):
    """모든 산출물 경로를 tmp로 격리(실계정 state 보호)."""

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
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def make_bot(self, universe, feed, pos=None, cash=10000.0):
        pf = pb.Portfolio(CFG)
        pf.cash = cash
        pf.pos = pos or {}
        with mock.patch.object(pb, "get_universe", return_value=universe):
            return pb.Bot(CFG, pf, feed), pf


class TestHoldingOutsideUniverse(PaperBotCase):

    def test_cycle_prices_holdings_even_if_not_in_universe(self):
        feed = FakeFeed({"AAA": 100.0, "ZZZ": 50.0})
        bot, pf = self.make_bot(["AAA"], feed,
                                pos={"ZZZ": {"entry": 100.0, "peak": 100.0,
                                             "shares": 5, "entry_at": "2026-09-29 23:00"}})
        bot.cycle(1400.0, allow_entry=False)
        self.assertIn("ZZZ", feed.asked[0],
                      "보유 종목이 시세 수집 대상에서 빠졌다 — 청산 판정 불가")

    def test_stop_fires_for_holding_outside_universe(self):
        """핵심 회귀 — 유니버스 밖 보유 종목도 손절이 발동해야 한다."""
        feed = FakeFeed({"AAA": 100.0, "ZZZ": 90.0})     # 진입 100 → 90 (-10%)
        bot, pf = self.make_bot(["AAA"], feed,
                                pos={"ZZZ": {"entry": 100.0, "peak": 100.0,
                                             "shares": 5, "entry_at": "2026-09-29 23:00"}})
        bot.cycle(1400.0, allow_entry=False)
        self.assertNotIn("ZZZ", pf.pos,
                         "유니버스에서 밀려난 보유 종목의 손절이 조용히 미발동했다")
        rows = [json.loads(l) for l in open(pb.TRADES, encoding="utf-8")]
        self.assertEqual(rows[-1]["sym"], "ZZZ")
        self.assertIn("손절", rows[-1]["reason"])

    def test_entries_come_only_from_universe(self):
        """보유 합집합이 진입 후보로 새지 않아야 한다."""
        feed = FakeFeed({"AAA": 100.0, "ZZZ": 100.0})
        bot, pf = self.make_bot(["AAA"], feed,
                                pos={"ZZZ": {"entry": 100.0, "peak": 100.0,
                                             "shares": 1, "entry_at": "x"}})
        pf.pos.pop("ZZZ")                 # 보유는 없지만 last_price엔 남는 상황
        bot.last_price["ZZZ"] = 100.0
        bot.cycle(1400.0, allow_entry=True)
        self.assertNotIn("ZZZ", pf.pos, "유니버스 밖 종목을 신규 진입했다")


class TestOvernightHold(PaperBotCase):

    def _run_one_session(self, cfg_over=None):
        cfg = dict(CFG)
        cfg.update(cfg_over or {})
        feed_state = {"open": True}
        feed = FakeFeed({"AAA": 100.0}, on_snapshot=lambda: feed_state.__setitem__("open", False))
        pf = pb.Portfolio(cfg)
        pf.cash = 1000.0
        pf.pos = {"AAA": {"entry": 100.0, "peak": 100.0, "shares": 5,
                          "entry_at": "2026-09-29 23:00"}}
        with mock.patch.object(pb, "get_universe", return_value=["AAA"]):
            bot = pb.Bot(cfg, pf, feed)
        with mock.patch.object(pb, "market_open", lambda *a, **k: feed_state["open"]), \
             mock.patch.object(pb.subprocess, "Popen"), \
             mock.patch.object(pb.time, "sleep", lambda *_: None), \
             mock.patch.object(pb, "fetch", lambda *a, **k: ser([500, 505])):
            bot.run()
        return bot, pf

    def test_market_close_keeps_positions(self):
        bot, pf = self._run_one_session()
        self.assertIn("AAA", pf.pos, "마감에 포지션이 청산됐다 — 오버나이트 전환 실패")
        rows = [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")]
        self.assertEqual(rows[-1]["reason"], "장마감")

    def test_legacy_flag_still_liquidates(self):
        """옛 동작 보존 — liquidate_on_close=true면 마감 전량청산."""
        bot, pf = self._run_one_session({"liquidate_on_close": True, "grace_min": 0})
        self.assertEqual(pf.pos, {}, "레거시 모드인데 청산되지 않았다")

    def test_daily_summary_values_carry_at_last_price(self):
        """캐리 포지션을 진입가로 평가하면 수익률이 거짓이 된다."""
        feed = FakeFeed({"AAA": 120.0})
        bot, pf = self.make_bot(["AAA"], feed, cash=0.0,
                                pos={"AAA": {"entry": 100.0, "peak": 100.0,
                                             "shares": 10, "entry_at": "x"}})
        bot.cycle(1400.0, allow_entry=False)          # last_price = 120
        bot.session_traded = True                     # 장중 순찰 있었음(#OPEN-PB ⓓ 전제)
        bot.day_start_krw = 100 * 10 * pf.fx0
        bot.open_equity_krw = 100 * 10 * pf.fx0
        with mock.patch.object(pb, "fetch", lambda *a, **k: ser([500, 505])):
            bot.daily_summary(1400.0, "장마감")
        rec = [json.loads(l) for l in open(pb.DAILY, encoding="utf-8")][-1]
        self.assertAlmostEqual(rec["day_return_pct"], 20.0, places=1,
                               msg="보유 종목이 진입가로 평가됐다(수익률 거짓)")


class TestUniverseCacheKey(PaperBotCase):

    def test_cache_key_is_et_trading_date(self):
        """KST 자정이 지나도 같은 미국장 세션이면 유니버스가 안 바뀌어야 한다."""
        kst_before = datetime(2026, 9, 30, 23, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        kst_after = datetime(2026, 10, 1, 1, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(pb.trading_date(kst_before), pb.trading_date(kst_after))
        self.assertEqual(pb.trading_date(kst_before), "2026-09-30")


if __name__ == "__main__":
    unittest.main()
