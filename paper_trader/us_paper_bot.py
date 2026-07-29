#!/usr/bin/env python3
"""
paper_trader/us_paper_bot.py — 미국주식 추세돌파 '종이(paper)' 자동매매 봇
========================================================
실제 돈 0원. 야후 시세를 폴링해 규칙대로 가상 매수/매도하고 원장에 기록한다.
목적: 실시간 자동매매 배관을 안전하게 경험 + 운영 로직(종료모드·유예) 검증 + OOS 데이터 축적.

규칙(과최적화 회피용 평범한 기본값):
  진입: 20일 신고가 돌파 + 50일 이평 위
  청산: 트레일링 스탑(고점 -10%) 또는 손절(진입가 -5%)
  최대 5종목 균등, side당 0.05% 비용 가정

종료 2가지(사용자 설계):
  ① 시간창(--window "22:30-05:00" KST): 창 안에서만 매매, 창 끝나면 전량 청산(오버나잇 X)
  ② 수동(Ctrl-C): 마무리 작업 후 종료. 손절 안 걸린 손실 포지션은 유예(최대 GRACE분)
     — 회복하면 청산, 아니면 유예 종료 시 청산. (손절은 언제나 예외 없이 즉시)

⚠️ 종이 전용. 실주문/실계좌 연결 없음. 검증 전 실투입 금지.
사용: python paper_trader/us_paper_bot.py --once        # 1회 점검(테스트)
      python paper_trader/us_paper_bot.py --poll 300    # 5분 폴링 루프
      python paper_trader/us_paper_bot.py --window 22:30-05:00 --poll 300
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
from guard_sweep import fetch  # noqa: E402  미국 심볼 일봉(최근값=현재가 근사)

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "ADBE", "CRM", "QCOM", "TXN", "INTC", "JPM", "V", "MA", "UNH", "JNJ",
    "XOM", "CVX", "WMT", "PG", "HD", "COST", "LLY", "ABBV", "KO", "PEP",
    "CAT", "BA", "GE", "DIS", "NKE",
]
HIGH_WINDOW, MA_WINDOW = 20, 50
TRAIL, STOP = 0.10, 0.05
MAX_POS, COST = 5, 0.0005
GRACE_MIN = 30  # 수동 종료 시 손실 포지션 유예(분)

STATE = ROOT / "data" / "paper_us_state.json"
LOG = ROOT / "log" / "paper_us.log"


def now_kst():
    return datetime.now()


def log(msg):
    line = f"{now_kst():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Portfolio:
    """가상 계좌: 현금 + 포지션. 파일로 영속화."""

    def __init__(self, capital=10000.0):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            self.cash = d["cash"]
            self.pos = d["pos"]
            self.realized = d.get("realized", 0.0)
        else:
            self.cash = capital
            self.pos = {}          # sym -> {entry, peak, shares}
            self.realized = 0.0
            self.save()

    def save(self):
        STATE.write_text(json.dumps(
            {"cash": self.cash, "pos": self.pos, "realized": self.realized,
             "updated": f"{now_kst():%Y-%m-%d %H:%M:%S}"}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def equity(self, price):
        return self.cash + sum(p["shares"] * price.get(s, p["entry"]) for s, p in self.pos.items())

    def buy(self, sym, px, budget):
        shares = (budget * (1 - COST)) / px
        self.cash -= budget
        self.pos[sym] = {"entry": px, "peak": px, "shares": shares}
        log(f"🟢 매수(종이) {sym} @ ${px:.2f}  예산 ${budget:.0f}")
        self.save()

    def sell(self, sym, px, reason):
        p = self.pos.pop(sym)
        proceeds = p["shares"] * px * (1 - COST)
        self.cash += proceeds
        ret = px / p["entry"] - 1
        self.realized += proceeds - p["shares"] * p["entry"]
        log(f"🔴 매도(종이) {sym} @ ${px:.2f}  수익 {ret*100:+.1f}%  ({reason})")
        self.save()


class QuoteFeed:
    """최근 3개월 일봉 → 신호계산용 히스토리 + 현재가(마지막 값)."""

    def snapshot(self, syms):
        out = {}
        for s in syms:
            try:
                ser = fetch(s, "3mo")
                if len(ser) >= MA_WINDOW:
                    out[s] = ser
            except Exception:
                continue
        return out


def breakout(ser):
    c = float(ser.iloc[-1])
    hi = float(ser.tail(HIGH_WINDOW).max())
    ma = float(ser.tail(MA_WINDOW).mean())
    return c >= hi and c > ma


def parse_window(w):
    if not w:
        return None
    a, b = w.split("-")
    return (dtime.fromisoformat(a), dtime.fromisoformat(b))


def in_window(win):
    if not win:
        return True
    t = now_kst().time()
    a, b = win
    return (a <= t <= b) if a <= b else (t >= a or t <= b)


class Bot:
    def __init__(self, pf, feed, window=None):
        self.pf = pf
        self.feed = feed
        self.window = window
        self.stop = False

    def cycle(self, allow_entry=True):
        data = self.feed.snapshot(WATCHLIST)
        price = {s: float(ser.iloc[-1]) for s, ser in data.items()}
        # 1) 청산 체크(손절/트레일)
        for sym in list(self.pf.pos.keys()):
            if sym not in price:
                continue
            c = price[sym]
            self.pf.pos[sym]["peak"] = max(self.pf.pos[sym]["peak"], c)
            p = self.pf.pos[sym]
            if c <= p["entry"] * (1 - STOP):
                self.pf.sell(sym, c, "손절 -5%")
            elif c <= p["peak"] * (1 - TRAIL):
                self.pf.sell(sym, c, f"트레일 -{int(TRAIL*100)}%")
        # 2) 진입 체크
        if allow_entry:
            eq = self.pf.equity(price)
            for sym in WATCHLIST:
                if len(self.pf.pos) >= MAX_POS:
                    break
                if sym in self.pf.pos or sym not in data:
                    continue
                if breakout(data[sym]):
                    budget = min(eq / MAX_POS, self.pf.cash)
                    if budget > 1:
                        self.pf.buy(sym, price[sym], budget)
        self.pf.save()
        return price

    def graceful_close(self, reason):
        """마무리: 승자·본전은 즉시 청산, 손절 안 걸린 손실은 유예 후 청산."""
        log(f"⏹ 종료 절차 시작 ({reason})")
        deadline = time.time() + GRACE_MIN * 60
        while self.pf.pos:
            data = self.feed.snapshot(list(self.pf.pos.keys()))
            price = {s: float(ser.iloc[-1]) for s, ser in data.items()}
            for sym in list(self.pf.pos.keys()):
                c = price.get(sym, self.pf.pos[sym]["entry"])
                ret = c / self.pf.pos[sym]["entry"] - 1
                if ret >= 0:
                    self.pf.sell(sym, c, "종료-청산")
                elif c <= self.pf.pos[sym]["entry"] * (1 - STOP):
                    self.pf.sell(sym, c, "종료-손절")
                elif time.time() >= deadline:
                    self.pf.sell(sym, c, "종료-유예만료")
                else:
                    log(f"⏳ {sym} 손실 {ret*100:+.1f}% → 회복 유예 대기(최대 {GRACE_MIN}분)")
            if self.pf.pos and time.time() < deadline:
                time.sleep(30)
        log(f"✅ 종료 완료. 현금 ${self.pf.cash:.0f} / 실현손익 ${self.pf.realized:+.0f}")

    def run(self, poll):
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        log(f"▶ 종이 봇 시작 (poll {poll}s, window {self.window or '없음'}) — Ctrl-C로 종료")
        was_in = True
        while not self.stop:
            inw = in_window(self.window)
            if not inw and was_in and self.pf.pos:
                self.graceful_close("시간창 종료(오버나잇 X)")
            was_in = inw
            price = self.cycle(allow_entry=inw)
            eq = self.pf.equity(price)
            log(f"… 순찰 완료 | 보유 {len(self.pf.pos)} | 평가액 ${eq:.0f}")
            for _ in range(int(poll)):
                if self.stop:
                    break
                time.sleep(1)
        self.graceful_close("수동 종료(Ctrl-C)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회만 실행(테스트)")
    ap.add_argument("--poll", type=int, default=300, help="폴링 간격(초)")
    ap.add_argument("--window", type=str, default="", help='매매 시간창 KST 예: "22:30-05:00"')
    ap.add_argument("--capital", type=float, default=10000.0, help="가상 자본($)")
    args = ap.parse_args()

    pf = Portfolio(args.capital)
    bot = Bot(pf, QuoteFeed(), parse_window(args.window))

    if args.once:
        log("🔎 --once 점검 실행(진입 포함)")
        price = bot.cycle(allow_entry=True)
        log(f"보유 {len(pf.pos)}종목 | 평가액 ${pf.equity(price):.0f} | 현금 ${pf.cash:.0f}")
        log(f"현재 보유: {list(pf.pos.keys()) or '없음'}")
    else:
        bot.run(args.poll)


if __name__ == "__main__":
    main()
