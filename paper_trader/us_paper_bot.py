#!/usr/bin/env python3
"""
paper_trader/us_paper_bot.py — 미국주식 추세돌파 '종이(paper)' 자동매매 봇
========================================================
실제 돈 0원. 야후 시세를 폴링해 규칙대로 가상 매수/매도하고, 분석용 데이터를 쌓는다.
설정은 paper_trader/config.json (개선 = 이 파일 값 수정 또는 코드 수정 후 재실행).

규칙(기본, 과최적화 회피용 평범값):
  진입: N일 신고가 돌파 + M일 이평 위 / 청산: 트레일링·손절 / 최대 K종목 균등
자본: 1000만원(원화). 미국주식이라 환율로 달러 환산해 운용, 평가액은 원화로 보고.

운영(사용자 설계):
  - 미국장 시간(ET 9:30~16:00) 에만 매매. 장 밖에선 대기.
  - **안 꺼도 장마감(16:00 ET, 약 05:00 KST) 때 자동 전량청산 + 일일요약 + 종료.**
  - Ctrl-C 로 언제든 수동 종료(마무리). 손절 안 걸린 손실은 GRACE분 유예 후 청산(손절은 즉시).

분석용 데이터(누적):
  - data/paper_us_state.json   현재 계좌(현금·보유)
  - data/paper_us_trades.jsonl 청산된 거래 1건=1줄(진입·청산·수익·사유)
  - data/paper_us_daily.jsonl  하루 1줄(시작·종료 평가액·일수익·거래수·vs SPY) ← 분석 핵심
  - log/paper_us.log           사람이 읽는 로그

⚠️ 종이 전용. 실주문/실계좌 연결 없음. 공정검증 전 실투입 금지.
사용: python paper_trader/us_paper_bot.py            # 켜두면 장마감때 자동정리
      python paper_trader/us_paper_bot.py --once     # 1회 점검(테스트)
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
from guard_sweep import fetch  # noqa: E402

CONFIG = ROOT / "paper_trader" / "config.json"
STATE = ROOT / "data" / "paper_us_state.json"
TRADES = ROOT / "data" / "paper_us_trades.jsonl"
DAILY = ROOT / "data" / "paper_us_daily.jsonl"
LIVE = ROOT / "data" / "paper_us_live.json"   # 웹 대시보드용 실시간 스냅샷
UNIV = ROOT / "data" / "paper_us_universe.json"  # 당일 스크리닝된 유니버스(캐시)
LOG = ROOT / "log" / "paper_us.log"
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def log(msg):
    line = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def get_fx():
    """USD/KRW 환율(원). 실패 시 1350 폴백."""
    try:
        return float(fetch("KRW=X", "5d").iloc[-1])
    except Exception:
        return 1350.0


def avg_dollar_volume(sym, days=20):
    """최근 days 거래일 평균 거래대금($) = 종가×거래량. 스크리닝용."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    q = raw["chart"]["result"][0]["indicators"]["quote"][0]
    pairs = [(c, v) for c, v in zip(q["close"], q["volume"]) if c and v][-days:]
    return sum(c * v for c, v in pairs) / len(pairs) if pairs else 0.0


def screen_universe(pool, size):
    """후보풀 → 거래대금 상위 size개."""
    scored = []
    for s in pool:
        try:
            dv = avg_dollar_volume(s)
            if dv > 0:
                scored.append((s, dv))
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    return [s for s, _ in scored[:size]]


def get_universe(cfg):
    """당일 스크리닝 유니버스(캐시). 하루 1회만 스크리닝하고 재사용."""
    today = f"{datetime.now(KST):%Y-%m-%d}"
    if UNIV.exists():
        try:
            d = json.loads(UNIV.read_text(encoding="utf-8"))
            if d.get("date") == today and d.get("universe"):
                return d["universe"]
        except Exception:
            pass
    log(f"🔎 유니버스 스크리닝: 후보 {len(cfg['candidate_pool'])}개 → 거래대금 상위 {cfg['universe_size']}")
    uni = screen_universe(cfg["candidate_pool"], cfg["universe_size"])
    if not uni:
        uni = cfg["candidate_pool"][:cfg["universe_size"]]  # 스크리닝 실패 폴백
    UNIV.parent.mkdir(parents=True, exist_ok=True)
    UNIV.write_text(json.dumps({"date": today, "universe": uni,
        "screened_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"   → {len(uni)}종목 선정: {', '.join(uni[:12])}…")
    return uni


def market_open(dt=None):
    """미국 정규장(ET 평일 09:30~16:00) 열려있나. DST는 zoneinfo가 처리."""
    t = (dt or datetime.now(ET)).astimezone(ET)
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= m < 16 * 60


class Portfolio:
    """가상 계좌: 내부는 달러(미국주식), 보고는 원화(환율)."""

    def __init__(self, cfg):
        self.cfg = cfg
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            self.cash = d["cash_usd"]
            self.pos = d["pos"]
            self.fx0 = d["fx0"]
            self.capital_krw = d["capital_krw"]
        else:
            fx = get_fx()
            self.capital_krw = cfg["capital_krw"]
            self.cash = self.capital_krw / fx     # 원화→달러 환산 매수여력
            self.pos = {}                          # sym -> {entry, peak, shares, entry_at}
            self.fx0 = fx
            self.save()
            log(f"🆕 신규 계좌: {self.capital_krw:,}원 ≈ ${self.cash:,.0f} (환율 {fx:,.1f})")

    def save(self):
        STATE.write_text(json.dumps({
            "cash_usd": self.cash, "pos": self.pos, "fx0": self.fx0,
            "capital_krw": self.capital_krw,
            "updated": f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def equity_usd(self, price):
        return self.cash + sum(p["shares"] * price.get(s, p["entry"]) for s, p in self.pos.items())

    def buy(self, sym, px, budget_usd):
        shares = (budget_usd * (1 - self.cfg["cost"])) / px
        self.cash -= budget_usd
        self.pos[sym] = {"entry": px, "peak": px, "shares": shares,
                         "entry_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}"}
        log(f"🟢 매수(종이) {sym} @ ${px:.2f}  예산 ${budget_usd:,.0f}")
        self.save()

    def sell(self, sym, px, reason, fx):
        p = self.pos.pop(sym)
        proceeds = p["shares"] * px * (1 - self.cfg["cost"])
        self.cash += proceeds
        ret = px / p["entry"] - 1
        pnl_krw = (px - p["entry"]) * p["shares"] * fx
        append_jsonl(TRADES, {
            "closed_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}", "sym": sym,
            "entry_px": round(p["entry"], 2), "exit_px": round(px, 2),
            "ret_pct": round(ret * 100, 2), "reason": reason,
            "entry_at": p["entry_at"], "pnl_krw": round(pnl_krw),
        })
        log(f"🔴 매도(종이) {sym} @ ${px:.2f}  수익 {ret*100:+.1f}%  ({reason})")
        self.save()


class Feed:
    def snapshot(self, syms, ma_window):
        out = {}
        for s in syms:
            try:
                ser = fetch(s, "3mo")
                if len(ser) >= ma_window:
                    out[s] = ser
            except Exception:
                continue
        return out


class Bot:
    def __init__(self, cfg, pf, feed):
        self.cfg = cfg
        self.pf = pf
        self.feed = feed
        self.stop = False
        self.day_start_krw = None
        self.universe = get_universe(cfg)   # 당일 거래대금 상위 스크리닝

    def _breakout(self, ser):
        c = float(ser.iloc[-1])
        return c >= float(ser.tail(self.cfg["high_window"]).max()) and \
            c > float(ser.tail(self.cfg["ma_window"]).mean())

    def _strength(self, ser):
        """신호 강도 = 최근 ~1개월(21거래일) 모멘텀. 강할수록 우선 매수."""
        if len(ser) > 21:
            return float(ser.iloc[-1] / ser.iloc[-21] - 1)
        return float(ser.iloc[-1] / ser.tail(self.cfg["ma_window"]).mean() - 1)

    def cycle(self, fx, allow_entry=True):
        data = self.feed.snapshot(self.universe, self.cfg["ma_window"])
        price = {s: float(ser.iloc[-1]) for s, ser in data.items()}
        # 청산(손절/트레일)
        for sym in list(self.pf.pos.keys()):
            if sym not in price:
                continue
            c = price[sym]
            self.pf.pos[sym]["peak"] = max(self.pf.pos[sym]["peak"], c)
            p = self.pf.pos[sym]
            if c <= p["entry"] * (1 - self.cfg["stop"]):
                self.pf.sell(sym, c, f"손절 -{int(self.cfg['stop']*100)}%", fx)
            elif c <= p["peak"] * (1 - self.cfg["trail"]):
                self.pf.sell(sym, c, f"트레일 -{int(self.cfg['trail']*100)}%", fx)
        # 진입: 돌파 후보를 신호강도 순으로 정렬해 빈 슬롯만큼 상위 매수
        if allow_entry:
            slots = self.cfg["max_pos"] - len(self.pf.pos)
            if slots > 0:
                cands = [(s, self._strength(data[s])) for s in self.universe
                         if s not in self.pf.pos and s in data and self._breakout(data[s])]
                cands.sort(key=lambda x: -x[1])
                eq = self.pf.equity_usd(price)
                for sym, _ in cands[:slots]:
                    budget = min(eq / self.cfg["max_pos"], self.pf.cash)
                    if budget > 1:
                        self.pf.buy(sym, price[sym], budget)
        return price

    def graceful_close(self, reason, fx):
        log(f"⏹ 종료 절차 ({reason}) — 승자·본전 즉시청산, 손실은 유예")
        deadline = time.time() + self.cfg["grace_min"] * 60
        while self.pf.pos and not self.stop_now(deadline):
            data = self.feed.snapshot(list(self.pf.pos.keys()), self.cfg["ma_window"])
            for sym in list(self.pf.pos.keys()):
                c = float(data[sym].iloc[-1]) if sym in data else self.pf.pos[sym]["entry"]
                ret = c / self.pf.pos[sym]["entry"] - 1
                if ret >= 0:
                    self.pf.sell(sym, c, "종료-청산", fx)
                elif c <= self.pf.pos[sym]["entry"] * (1 - self.cfg["stop"]):
                    self.pf.sell(sym, c, "종료-손절", fx)
                elif time.time() >= deadline:
                    self.pf.sell(sym, c, "종료-유예만료", fx)
                else:
                    log(f"⏳ {sym} 손실 {ret*100:+.1f}% 회복 유예중")
            if self.pf.pos and time.time() < deadline:
                time.sleep(30)
        # 남은 것 강제 청산
        if self.pf.pos:
            data = self.feed.snapshot(list(self.pf.pos.keys()), self.cfg["ma_window"])
            for sym in list(self.pf.pos.keys()):
                c = float(data[sym].iloc[-1]) if sym in data else self.pf.pos[sym]["entry"]
                self.pf.sell(sym, c, "종료-강제청산", fx)

    def stop_now(self, deadline):
        return False

    def daily_summary(self, fx, reason):
        """하루 요약 1줄 기록(분석 핵심). vs SPY 포함."""
        eq_krw = self.pf.equity_usd({}) * self.pf.fx0
        try:
            spy = fetch("SPY", "5d")
            spy_day = round((float(spy.iloc[-1]) / float(spy.iloc[-2]) - 1) * 100, 2)
        except Exception:
            spy_day = None
        start = self.day_start_krw or self.pf.capital_krw
        rec = {
            "date": f"{datetime.now(KST):%Y-%m-%d}", "reason": reason,
            "start_equity_krw": round(start), "end_equity_krw": round(eq_krw),
            "day_return_pct": round((eq_krw / start - 1) * 100, 2) if start else 0,
            "cum_return_pct": round((eq_krw / self.pf.capital_krw - 1) * 100, 2),
            "fx": round(fx, 1), "spy_day_pct": spy_day,
        }
        append_jsonl(DAILY, rec)
        log(f"📊 일일요약: 평가액 {rec['end_equity_krw']:,}원 "
            f"(당일 {rec['day_return_pct']:+.2f}% / 누적 {rec['cum_return_pct']:+.2f}%, "
            f"SPY {spy_day if spy_day is not None else '?'}%)")

    def write_live(self, price, fx, running=True):
        """웹 대시보드용 실시간 스냅샷 저장. 평가액은 진입환율(fx0) 고정 = 순수 매매성과."""
        eq_usd = self.pf.equity_usd(price)
        fx0 = self.pf.fx0
        positions = []
        for s, p in self.pf.pos.items():
            cur = price.get(s, p["entry"])
            positions.append({
                "sym": s, "entry": round(p["entry"], 2), "current": round(cur, 2),
                "ret_pct": round((cur / p["entry"] - 1) * 100, 2),
                "value_usd": round(p["shares"] * cur, 2),
                "value_krw": round(p["shares"] * cur * fx0),
            })
        LIVE.parent.mkdir(parents=True, exist_ok=True)
        LIVE.write_text(json.dumps({
            "running": running, "market_open": market_open(),
            "updated": f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}",
            "equity_usd": round(eq_usd, 2), "cash_usd": round(self.pf.cash, 2),
            "capital_usd": round(self.pf.capital_krw / fx0, 2),
            "equity_krw": round(eq_usd * fx0), "cash_krw": round(self.pf.cash * fx0),
            "capital_krw": self.pf.capital_krw, "universe_n": len(self.universe),
            "cum_return_pct": round((eq_usd * fx0 / self.pf.capital_krw - 1) * 100, 2),
            "fx": round(fx, 1), "fx0": round(fx0, 1), "positions": positions,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        poll = self.cfg["poll_sec"]
        log(f"▶ 종이 봇 시작 | 자본 {self.pf.capital_krw:,}원 | poll {poll}s")
        log("   종료: Ctrl-C (수동) / 안 꺼도 미국장 마감(약 05:00 KST) 자동정리")
        fx = get_fx()
        self.day_start_krw = self.pf.equity_usd({}) * self.pf.fx0
        was_open = False
        while not self.stop:
            fx = get_fx()
            is_open = market_open()
            if is_open:
                if not was_open:
                    log("🔔 미국장 개장 — 매매 시작")
                    self.day_start_krw = self.pf.equity_usd({}) * self.pf.fx0
                price = self.cycle(fx, allow_entry=True)
                self.write_live(price, fx, True)
                log(f"… 순찰 | 보유 {len(self.pf.pos)} | 평가액 {self.pf.equity_usd(price)*self.pf.fx0:,.0f}원")
            elif was_open:
                # 방금 장마감 → 자동 정리 + 요약 + 종료
                log("🔔 미국장 마감 — 자동 정리 시작")
                self.graceful_close("장마감", fx)
                self.daily_summary(fx, "장마감")
                self.write_live({}, fx, False)
                log("💤 하루 종료. 내일 다시 켜줘.")
                return
            else:
                self.write_live({}, fx, True)
                log("… 장 열림 대기중(미국 정규장 밖)")
            was_open = is_open
            for _ in range(int(poll)):
                if self.stop:
                    break
                time.sleep(1)
        # 수동 종료
        self.graceful_close("수동 종료(Ctrl-C)", fx)
        self.daily_summary(fx, "수동종료")
        self.write_live({}, fx, False)
        log("✅ 종료 완료.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회 점검(테스트)")
    args = ap.parse_args()
    cfg = load_config()
    pf = Portfolio(cfg)
    bot = Bot(cfg, pf, Feed())
    if args.once:
        fx = get_fx()
        log(f"🔎 --once 점검 (장 {'열림' if market_open() else '닫힘'}, 환율 {fx:,.1f})")
        price = bot.cycle(fx, allow_entry=market_open())
        bot.write_live(price, fx, False)
        log(f"보유 {list(pf.pos.keys()) or '없음'} | 평가액 {pf.equity_usd(price)*pf.fx0:,.0f}원")
    else:
        bot.run()


if __name__ == "__main__":
    main()
