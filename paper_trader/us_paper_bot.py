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
  - **장마감(16:00 ET, 약 05:00 KST) 때 일일요약 기록 + 종료. 포지션은 다음 날로 넘긴다(오버나이트).**
  - Ctrl-C 로 언제든 수동 종료. 종료해도 포지션은 state에 남아 다음 실행이 이어받는다.
  - `liquidate_on_close: true` 로 두면 옛 동작(마감 전량청산)으로 되돌아간다.

[2026-09-30 오버나이트 전환 — 왜]
  전략 정의는 스윙(20일 신고가 돌파 + 50일 이평 / 트레일 -10% / 손절 -5%)인데 구현이 매일
  마감에 전량청산하는 데이트레이드였다. 대형주 일간변동 1~2%로는 -10% 트레일이 한 세션 안에
  발동할 수 없어 **청산 규칙이 장식**이었고(실측: 거래 7건 전부 '종료-*' 청산, 손절·트레일 0건),
  매일 전량 회전해 왕복비용 0.30%/일이 구조적으로 깎였다. 규칙이 작동할 시간을 주도록 전환.

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
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def upsert_jsonl_by_date(path, rec):
    """[#OPEN-PB ⓓ] 같은 날짜 레코드를 덮어쓴다(append 금지).

    기존엔 종료 경로마다 append해서 같은 날 두 번 켜면 두 줄이 쌓이고, 잠깐 켰다 끄면
    가짜 '하루'가 생겼다(실측: 2026-07-30 수동종료 +0.02%). '하루=한 줄'을 보장한다.
    """
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date") != rec["date"]:
                rows.append(r)
            else:
                rec["sessions"] = int(r.get("sessions", 1)) + 1
    rows.append(rec)
    rows.sort(key=lambda r: r.get("date", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, path)


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


MIN_SCREEN_COVERAGE = 0.70   # [③] 후보풀 중 이 비율 미만만 조회되면 신뢰할 수 없다


def screen_universe(pool, size):
    """후보풀 → 거래대금 상위 size개. (선정목록, 조회성공수) 반환.

    [③ 2026-10-01] 이전엔 종목별 실패를 `except: continue`로 삼키고 **몇 개가 실패했는지
    세지도 않았다.** 절반이 실패해도 '성공한 것 중 상위 40'으로 조용히 진행되고,
    전부 실패하면 후보풀 앞 40개(기술주 편중)로 무경고 폴백했다. rambdaA는 fix24로
    커버리지 70% 가드를 넣어둔 바로 그 클래스인데 여기엔 없었다.
    """
    scored, failed = [], []
    for s in pool:
        try:
            dv = avg_dollar_volume(s)
            if dv > 0:
                scored.append((s, dv))
            else:
                failed.append(s)
        except Exception:
            failed.append(s)
    if failed:
        log(f"⚠️ 스크리닝 실패 {len(failed)}/{len(pool)}종: {failed[:8]}"
            f"{'…' if len(failed) > 8 else ''}")
    scored.sort(key=lambda x: -x[1])
    return [s for s, _ in scored[:size]], len(scored)


def get_universe(cfg):
    """당일 스크리닝 유니버스(캐시). 하루 1회만 스크리닝하고 재사용."""
    today = trading_date()   # ET 거래일 — 장중 캐시 만료 방지
    if UNIV.exists():
        try:
            d = json.loads(UNIV.read_text(encoding="utf-8"))
            if d.get("date") == today and d.get("universe"):
                return d["universe"]
        except Exception:
            pass
    pool = cfg["candidate_pool"]
    log(f"🔎 유니버스 스크리닝: 후보 {len(pool)}개 → 거래대금 상위 {cfg['universe_size']}")
    uni, ok_n = screen_universe(pool, cfg["universe_size"])
    coverage = ok_n / len(pool) if pool else 0.0
    if coverage < MIN_SCREEN_COVERAGE:
        # [③] 조용히 넘어가지 않는다 — 이 유니버스로 뽑은 top5는 신뢰도가 낮다.
        log(f"🚨 스크리닝 커버리지 부족: {ok_n}/{len(pool)} ({coverage:.0%} < "
            f"{MIN_SCREEN_COVERAGE:.0%}) — 유니버스 신뢰도 낮음(진입 판단 시 감안)")
    else:
        log(f"   커버리지 {ok_n}/{len(pool)} ({coverage:.0%})")
    if not uni:
        uni = pool[:cfg["universe_size"]]  # 스크리닝 전멸 폴백
        log(f"🚨 스크리닝 전멸 → 후보풀 앞 {len(uni)}종으로 폴백(기술주 편중 주의)")
    UNIV.parent.mkdir(parents=True, exist_ok=True)
    UNIV.write_text(json.dumps({"date": today, "universe": uni,
        "screened_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"   → {len(uni)}종목 선정: {', '.join(uni[:12])}…")
    return uni


def trading_date(dt=None):
    """ET 기준 날짜. 유니버스 캐시 키로 쓴다.

    KST 날짜를 쓰면 미국장이 KST 두 날짜에 걸치므로(22:30~05:00) **장중 자정에 캐시가 만료돼
    유니버스가 통째로 재선정**된다. 그러면 진입 후보가 장중에 바뀌어 재현성이 깨지고, 보유 종목이
    새 유니버스에서 빠질 수 있다. ET 날짜는 한 세션 내내 고정이다.
    """
    return f"{(dt or datetime.now(ET)).astimezone(ET):%Y-%m-%d}"


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
        self.last_close_equity_krw = None   # [#OPEN-PB ⓑ] 전일 마감 평가액(갭 계상 기준)
        self.last_exit = {}                 # [② 재진입 쿨다운] sym → 마지막 청산 ET 거래일
        # [2026-10-01 ⓑ-2] 당일 수익률 기준점을 state에 들고 간다. 장중에 봇을 껐다 켜면
        # 기준점이 '재시작 시각 평가액'으로 다시 잡혀 그 사이 성과가 어느 날에도 안 들어갔다
        # (#OPEN-PB ⓑ와 같은 클래스 — 재시작이 잦은 운용 방식에서 매번 구멍이 난다).
        self.day_start_date = None          # 기준점이 속한 ET 거래일
        self.day_start_krw = None           # 그날 기준 평가액(= 전일 마감 또는 당일 개장)
        self.day_open_krw = None            # 당일 개장 평가액(갭/장중 분해용)
        d = self._load_state()
        if d is not None:
            self.cash = d["cash_usd"]
            self.pos = d["pos"]
            self.fx0 = d["fx0"]
            self.capital_krw = d["capital_krw"]
            self.last_close_equity_krw = d.get("last_close_equity_krw")
            self.last_exit = d.get("last_exit") or {}     # [②] sym → 마지막 청산 ET 거래일
            self.day_start_date = d.get("day_start_date")
            self.day_start_krw = d.get("day_start_krw")
            self.day_open_krw = d.get("day_open_krw")
        else:
            fx = get_fx()
            self.capital_krw = cfg["capital_krw"]
            self.cash = self.capital_krw / fx     # 원화→달러 환산 매수여력
            self.pos = {}                          # sym -> {entry, peak, shares, entry_at}
            self.fx0 = fx
            self.save()
            log(f"🆕 신규 계좌: {self.capital_krw:,}원 ≈ ${self.cash:,.0f} (환율 {fx:,.1f})")

    @staticmethod
    def _load_state():
        """[#OPEN-PB ⓐ] 상태 로드 — 손상 시 백업으로 복구. 둘 다 깨졌으면 조용히
        새 계좌를 만들지 않고 큰 소리로 멈춘다(포지션 유실을 '정상 시작'으로 위장 금지)."""
        for path, label in ((STATE, "본파일"), (STATE.with_suffix(".bak"), "백업")):
            if not path.exists():
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                if label == "백업":
                    log(f"⚠️ 상태파일 손상 → {label}에서 복구: {path.name}")
                return d
            except Exception as e:
                log(f"🚨 상태파일 파싱 실패({label} {path.name}): {e}")
        if STATE.exists() or STATE.with_suffix(".bak").exists():
            raise RuntimeError(
                "상태파일이 모두 손상됐습니다. 새 계좌를 자동 생성하지 않습니다 — "
                f"{STATE} / {STATE.with_suffix('.bak')} 를 확인하거나 수동 삭제 후 재시작하세요.")
        return None

    def save(self):
        """[#OPEN-PB ⓐ] 원자적 저장. 오버나이트 전환 이후 이 파일이 포지션의 유일한
        기록이므로, 쓰는 중 프로세스가 죽어도 이전 상태가 남아야 한다(tmp → os.replace)."""
        payload = json.dumps({
            "cash_usd": self.cash, "pos": self.pos, "fx0": self.fx0,
            "capital_krw": self.capital_krw,
            "last_close_equity_krw": self.last_close_equity_krw,
            "last_exit": self.last_exit,
            "day_start_date": self.day_start_date,
            "day_start_krw": self.day_start_krw,
            "day_open_krw": self.day_open_krw,
            "updated": f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}",
        }, ensure_ascii=False, indent=2)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        if STATE.exists():
            try:
                shutil.copy2(STATE, STATE.with_suffix(".bak"))
            except Exception:
                pass
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, STATE)      # 원자적 교체

    def equity_usd(self, price):
        return self.cash + sum(p["shares"] * price.get(s, p["entry"]) for s, p in self.pos.items())

    def buy(self, sym, px, budget_usd):
        slip = self.cfg.get("slippage", 0.0)
        fill = px * (1 + slip)                    # 슬리피지: 살 때 불리하게
        shares = budget_usd / (fill * (1 + self.cfg["cost"]))
        if self.cfg.get("whole_shares"):
            shares = int(shares)                  # 정수주(한국 브로커 현실)
        if shares < 1:
            return
        spent = shares * fill
        self.cash -= spent + spent * self.cfg["cost"]
        self.pos[sym] = {"entry": round(fill, 4), "peak": px, "shares": shares,
                         "entry_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}"}
        log(f"🟢 매수(종이) {sym} {shares}주 @ ${fill:.2f} (슬리피지반영)")
        self.save()

    def sell(self, sym, px, reason, fx):
        p = self.pos.pop(sym)
        slip = self.cfg.get("slippage", 0.0)
        fill = px * (1 - slip)                    # 슬리피지: 팔 때 불리하게
        gross = p["shares"] * fill
        self.cash += gross - gross * self.cfg["cost"]
        ret = fill / p["entry"] - 1
        pnl_krw = (fill - p["entry"]) * p["shares"] * fx
        append_jsonl(TRADES, {
            "closed_at": f"{datetime.now(KST):%Y-%m-%d %H:%M}", "sym": sym,
            "shares": p["shares"], "entry_px": round(p["entry"], 2), "exit_px": round(fill, 2),
            "ret_pct": round(ret * 100, 2), "reason": reason,
            "entry_at": p["entry_at"], "pnl_krw": round(pnl_krw),
        })
        # [② 재진입 쿨다운] 청산일을 남긴다 — 손절 직후 같은 종목을 다시 사서
        # 왕복비용(0.30%)만 물리는 핑퐁을 막기 위한 근거.
        self.last_exit[sym] = trading_date()
        log(f"🔴 매도(종이) {sym} {p['shares']}주 @ ${fill:.2f}  수익 {ret*100:+.1f}%  ({reason})")
        self.save()


class Feed:
    def snapshot(self, syms, ma_window):
        """40종목을 병렬로 수집(순차→동시). 한 사이클 시간·throttling 위험 대폭 감소."""
        def one(s):
            try:
                ser = fetch(s, "3mo")
                return (s, ser) if len(ser) >= ma_window else None
            except Exception:
                return None
        out = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(one, syms):
                if r:
                    out[r[0]] = r[1]
        return out


class Bot:
    def __init__(self, cfg, pf, feed):
        self.cfg = cfg
        self.pf = pf
        self.feed = feed
        self.stop = False
        self.day_start_krw = None
        self.need_day_start = False
        # [오버나이트] 마지막으로 관측한 종목별 시세. 보유 종목을 '진입가'로 평가하는
        # equity_usd(price={}) 폴백을 쓰지 않기 위해 유지한다(수익률이 왜곡됨).
        self.last_price = {}
        # [#OPEN-PB ⓒ] 시세별 관측시각. 값만 들고 있으면 며칠 전 시세로 평가해도 조용하다
        # (analyze_returns fix41에서 고친 것과 같은 결함을 여기서 반복했음).
        self.last_seen = {}
        self.open_equity_krw = None        # [ⓑ] 당일 개장 첫 순찰 평가액
        self.session_traded = False        # [ⓓ] 이 세션이 장중 순찰을 한 번이라도 했나
        self._last_dropped = None          # [③] 직전 순찰의 유니버스 누락 목록(변화시에만 로그)
        self.universe = get_universe(cfg)   # 당일 거래대금 상위 스크리닝

    def _breakout(self, ser):
        """[2026-10-01 ①] 오늘(진행중) 바를 **제외한** 과거 구간과 비교한다.

        이전엔 `ser.tail(20).max()` 안에 오늘 값 c 자신이 들어 있어(자기참조):
          - 실질 '직전 19일' 돌파가 되는 off-by-one이었고,
          - 더 중요하게는 **장중 가격 vs 일간 종가**를 비교해 언제 쳐다보느냐에 따라
            신호가 생겼다 사라졌다 했다(재현성 없음).
        개장 직후엔 c가 사실상 시가라, 조건이 "시가 ≥ 직전 19일 종가 최고치"가 되어
        **갭 상승 종목을 자동으로 추격 매수**하는 규칙으로 작동했다(2026-09-30 실측:
        개장 2초 만에 5슬롯 만석). 오버나이트 보유로 바뀌며 진입 품질의 비중이 커져 교정한다.
        50일 이평도 같은 이유로 오늘 바를 뺀다.
        """
        c = float(ser.iloc[-1])
        hist = ser.iloc[:-1]                      # 오늘(진행중) 바 제외
        need = max(self.cfg["high_window"], self.cfg["ma_window"])
        if len(hist) < need:
            return False
        return c >= float(hist.tail(self.cfg["high_window"]).max()) and \
            c > float(hist.tail(self.cfg["ma_window"]).mean())

    def _strength(self, ser):
        """신호 강도 = 최근 ~1개월(21거래일) 모멘텀. 강할수록 우선 매수."""
        if len(ser) > 21:
            return float(ser.iloc[-1] / ser.iloc[-21] - 1)
        return float(ser.iloc[-1] / ser.tail(self.cfg["ma_window"]).mean() - 1)

    def _in_cooldown(self, sym) -> bool:
        """[②] 최근 청산한 종목인가. 기본 1일 = 같은 ET 거래일 재진입 금지.

        손절(-5%)로 나간 종목이 여전히 돌파 조건을 만족하면 몇 분 뒤 다시 살 수 있었고
        (진입 필터가 '보유중이 아님'만 봤다), 그때마다 왕복 0.30%가 나갔다.
        """
        days = int(self.cfg.get("reentry_cooldown_days", 1))
        if days <= 0:
            return False
        last = self.pf.last_exit.get(sym)
        if not last:
            return False
        try:
            gone = (datetime.strptime(trading_date(), "%Y-%m-%d")
                    - datetime.strptime(last, "%Y-%m-%d")).days
        except Exception:
            return False
        return gone < days

    def cycle(self, fx, allow_entry=True):
        # [오버나이트 필수] 시세 수집 대상 = 유니버스 ∪ 보유종목.
        # 유니버스만 긁으면, 보유 종목이 유니버스에서 밀려난 순간 price에 안 담기고
        # 아래 청산 루프가 `continue`로 건너뛰어 **손절·트레일이 조용히 영구 미발동**한다.
        # 당일청산 시절엔 몇 시간이면 끝나 잘 안 드러났지만, 며칠 들고 가면 치명적이다.
        syms = list(dict.fromkeys(list(self.universe) + list(self.pf.pos.keys())))
        data = self.feed.snapshot(syms, self.cfg["ma_window"])
        price = {s: float(ser.iloc[-1]) for s, ser in data.items()}
        self.last_price.update(price)
        now = time.time()
        for sym in price:
            self.last_seen[sym] = now
        missing = [s for s in self.pf.pos if s not in price]
        if missing:
            log(f"⚠️ 보유 종목 시세 수집 실패 {missing} — 이번 순찰에서 청산 판정 불가")
        # [③] 유니버스 종목의 조용한 탈락(히스토리 부족·조회 실패)도 드러낸다.
        # 후보군이 줄어든 채 top5를 뽑고 있는데 아무도 모르는 상황을 막는다.
        dropped = sorted(s for s in self.universe if s not in price)
        if dropped != self._last_dropped:
            if dropped:
                log(f"⚠️ 유니버스 시세 누락 {len(dropped)}/{len(self.universe)}종: "
                    f"{dropped[:8]}{'…' if len(dropped) > 8 else ''}")
            else:
                log("✅ 유니버스 시세 전종목 수집 정상")
            self._last_dropped = dropped
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
                # 진입 후보는 유니버스에서만 고른다(보유 합집합이 후보로 새지 않게)
                # [② 재진입 쿨다운] 최근 청산한 종목은 쿨다운 동안 제외한다.
                cands = [(s, self._strength(data[s])) for s in self.universe
                         if s not in self.pf.pos and s in data
                         and not self._in_cooldown(s) and self._breakout(data[s])]
                blocked = [s for s in self.universe
                           if s not in self.pf.pos and s in data
                           and self._in_cooldown(s) and self._breakout(data[s])]
                if blocked:
                    log(f"⏸ 재진입 쿨다운으로 제외: {blocked}")
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
        """하루 요약 1줄 기록(분석 핵심). vs SPY·갭 분해·신선도 표기 포함.

        [#OPEN-PB ⓑ] 수익률 기준은 '전일 마감 평가액' — 그래야 오버나이트 갭이 일별
        수익률에 들어가고 일별 수익률의 곱이 자산곡선과 맞는다. 갭/장중을 분리 기록해
        어느 쪽에서 성과가 났는지 사후에 가를 수 있게 한다.
        [#OPEN-PB ⓒ] 캐리 포지션 평가에 쓴 시세가 낡았으면 수치를 내되 stale로 표기한다.
        [#OPEN-PB ⓓ] 장중 순찰이 한 번도 없던 세션은 '하루'를 만들지 않는다.
        """
        if not self.session_traded:
            log("ℹ️ 장중 순찰 없이 종료 — 일일요약 기록 생략(가짜 하루 방지)")
            return
        eq_krw = self.pf.equity_usd(self.last_price) * self.pf.fx0

        # 신선도 검사: 보유 종목 평가에 쓴 시세가 얼마나 낡았나
        stale_limit = max(int(self.cfg.get("poll_sec", 180)) * 3, 1800)
        now = time.time()
        stale = sorted(s for s in self.pf.pos
                       if now - self.last_seen.get(s, 0) > stale_limit)
        if stale:
            log(f"⚠️ 평가 시세가 낡은 보유 종목 {stale} — 요약을 stale로 표기")

        try:
            spy = fetch("SPY", "5d")
            spy_day = round((float(spy.iloc[-1]) / float(spy.iloc[-2]) - 1) * 100, 2)
        except Exception:
            spy_day = None

        base = self.day_start_krw or self.pf.capital_krw
        op = self.open_equity_krw
        rec = {
            "date": f"{datetime.now(KST):%Y-%m-%d}", "reason": reason,
            "regime": "overnight" if not self.cfg.get("liquidate_on_close") else "daytrade",
            "sessions": 1,
            "start_equity_krw": round(base),          # = 전일 마감 평가액(있으면)
            "open_equity_krw": round(op) if op else None,
            "end_equity_krw": round(eq_krw),
            # 전일 마감 대비(갭 포함) — 이 값들의 곱이 자산곡선과 일치해야 한다
            "day_return_pct": round((eq_krw / base - 1) * 100, 2) if base else 0,
            # 분해: 갭(전일마감→개장) / 장중(개장→마감)
            "gap_pct": round((op / base - 1) * 100, 2) if (op and base) else None,
            "intraday_pct": round((eq_krw / op - 1) * 100, 2) if op else None,
            "cum_return_pct": round((eq_krw / self.pf.capital_krw - 1) * 100, 2),
            "positions_held": sorted(self.pf.pos.keys()),
            "stale_priced": stale or None,
            "fx": round(fx, 1), "spy_day_pct": spy_day,
        }
        upsert_jsonl_by_date(DAILY, rec)
        # 다음 날의 갭 계산 기준 — 반드시 요약 직후 저장
        self.pf.last_close_equity_krw = eq_krw
        self.pf.save()
        gap_txt = f" [갭 {rec['gap_pct']:+.2f}% / 장중 {rec['intraday_pct']:+.2f}%]" \
            if rec["gap_pct"] is not None else ""
        log(f"📊 일일요약: 평가액 {rec['end_equity_krw']:,}원 "
            f"(당일 {rec['day_return_pct']:+.2f}% / 누적 {rec['cum_return_pct']:+.2f}%, "
            f"SPY {spy_day if spy_day is not None else '?'}%){gap_txt}"
            f"{' ⚠️stale' if stale else ''}")

    def write_live(self, price, fx, running=True):
        """웹 대시보드용 실시간 스냅샷 저장. 평가액은 진입환율(fx0) 고정 = 순수 매매성과."""
        eq_usd = self.pf.equity_usd(price)
        fx0 = self.pf.fx0
        positions = []
        for s, p in self.pf.pos.items():
            cur = price.get(s, p["entry"])
            positions.append({
                "sym": s, "shares": round(p["shares"], 3),
                "entry": round(p["entry"], 2), "current": round(cur, 2),
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
        # 봇이 스스로 절전 방지(화면보호기에도 순찰 유지). 봇 종료 시 caffeinate 자동 해제.
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except Exception:
            pass
        poll = self.cfg["poll_sec"]
        log(f"▶ 종이 봇 시작 | 자본 {self.pf.capital_krw:,}원 | poll {poll}s")
        mode = "마감 전량청산" if self.cfg.get("liquidate_on_close") else "보유 유지(오버나이트)"
        log(f"   장마감(약 05:00 KST) 시 {mode} 후 종료 / Ctrl-C 수동 종료")
        fx = get_fx()
        # day_start는 첫 순찰에서 실시세로 확정한다(진입가 폴백 방지)
        self.day_start_krw = None
        self.need_day_start = True
        was_open = False
        if self.pf.pos:
            log(f"📦 이전 세션 포지션 이어받음: {list(self.pf.pos.keys())} (오버나이트)")
        while not self.stop:
            fx = get_fx()
            is_open = market_open()
            if is_open:
                if not was_open:
                    log("🔔 미국장 개장 — 매매 시작")
                    self.need_day_start = True
                price = self.cycle(fx, allow_entry=True)
                self.session_traded = True
                if self.need_day_start:
                    today = trading_date()
                    if self.pf.day_start_date == today and self.pf.day_start_krw:
                        # [ⓑ-2] 같은 거래일에 재시작한 경우 — 기존 기준점을 이어받는다.
                        # 새로 잡으면 껐다 켜기 전 구간이 어느 날에도 계상되지 않는다.
                        self.day_start_krw = self.pf.day_start_krw
                        self.open_equity_krw = self.pf.day_open_krw or self.day_start_krw
                        log(f"↩️ 같은 거래일 재시작 — 당일 기준점 이어받음 "
                            f"({self.day_start_krw:,.0f}원)")
                    else:
                        self.open_equity_krw = self.pf.equity_usd(price) * self.pf.fx0
                        # [#OPEN-PB ⓑ] 기준은 '전일 마감 평가액' — 그래야 오버나이트 갭이
                        # 일별 수익률에 들어간다. 없으면(첫 실행) 개장 평가액으로 대체.
                        self.day_start_krw = self.pf.last_close_equity_krw or self.open_equity_krw
                        self.pf.day_start_date = today
                        self.pf.day_start_krw = self.day_start_krw
                        self.pf.day_open_krw = self.open_equity_krw
                        self.pf.save()
                    self.need_day_start = False
                self.write_live(price, fx, True)
                log(f"… 순찰 | 보유 {len(self.pf.pos)} | 평가액 {self.pf.equity_usd(price)*self.pf.fx0:,.0f}원")
            elif was_open:
                # 방금 장마감. 기본은 **보유 유지(오버나이트)** — 요약만 남기고 종료한다.
                if self.cfg.get("liquidate_on_close"):
                    log("🔔 미국장 마감 — 전량청산 모드(liquidate_on_close=true)")
                    self.graceful_close("장마감", fx)
                else:
                    log(f"🔔 미국장 마감 — 보유 {len(self.pf.pos)}종목 다음 날로 이어감(오버나이트)")
                self.daily_summary(fx, "장마감")
                self.write_live(self.last_price, fx, False)
                log("💤 하루 종료. 포지션은 state에 유지됨 — 내일 다시 켜면 이어받는다.")
                return
            else:
                # 장 밖: 매매하지 않는다. 보유 종목 시세만 읽어 대시보드를 최신화한다.
                if self.pf.pos:
                    try:
                        data = self.feed.snapshot(list(self.pf.pos.keys()), self.cfg["ma_window"])
                        fresh = {s: float(ser.iloc[-1]) for s, ser in data.items()}
                        self.last_price.update(fresh)
                        for sym in fresh:
                            self.last_seen[sym] = time.time()
                    except Exception:
                        pass
                self.write_live(self.last_price, fx, True)
                log(f"… 장 열림 대기중(미국 정규장 밖) | 보유 {len(self.pf.pos)}")
            was_open = is_open
            for _ in range(int(poll)):
                if self.stop:
                    break
                time.sleep(1)
        # 수동 종료 — 포지션을 청산하지 않는다. 봇을 끄는 행위가 포트폴리오를 바꾸면
        # 성과가 '언제 껐는지'에 좌우돼 측정 자체가 무의미해진다.
        if self.cfg.get("liquidate_on_close"):
            self.graceful_close("수동 종료(Ctrl-C)", fx)
        elif self.pf.pos:
            log(f"⏹ 수동 종료 — 보유 {len(self.pf.pos)}종목 유지(다음 실행이 이어받음)")
        self.daily_summary(fx, "수동종료")
        self.write_live(self.last_price, fx, False)
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
        bot.write_live(price, fx, False)   # last_price는 cycle이 갱신함
        log(f"보유 {list(pf.pos.keys()) or '없음'} | 평가액 {pf.equity_usd(price)*pf.fx0:,.0f}원")
    else:
        bot.run()


if __name__ == "__main__":
    main()
