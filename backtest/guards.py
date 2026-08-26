"""
backtest/guards.py — 섀도우 전진검증용 시장 가드 (관측 전용, 자본 0)
================================================================
왜 분리했나:
  shadow_forward 는 가드를 `use_sma: bool` 로 표현하고 있었다. 후보가 2개일 때는
  괜찮지만 3개째부터는 불리언 조합이 폭발한다. 가드를 객체로 세우면 후보 추가가
  클래스 하나 추가로 끝나고, 각 후보의 사전고정 파라미터를 `describe()` 로
  원장에 그대로 박아 사후 변경을 막을 수 있다(AUDIT ③ STEP B).

⚠️ 여기 있는 어떤 클래스도 프로덕션(rambdaA/rambdaB)에서 import 되지 않는다.
   실매매 로직은 무변경이며, 이 파일은 오로지 '만약 이랬다면'을 채점하기 위한 것이다.

임계값 출처 (사후에 바꾸지 않는다):
  - DDGuard        : 현행 프로덕션 값 그대로 (-8% / 20일). 기준선.
  - ComboGuard     : 현행 + SMA120 이탈. 기존 후보 유지.
  - SigmaDDGuard   : 임계를 변동성에 비례시킨다. 계수 K는 **손으로 고른 값이 아니라**
                     "폭락 이전 평상시 변동성에서 현행 -8%와 같은 엄격도를 재현하는 값"
                     으로 역산한다(아래 K_SIGMA 참조). 이번 폭락 데이터에 맞춘 값이 아님.
  - DailyCircuitBreaker : 주간 판정은 현행 그대로 두고, 구간 중 매일 '비상' 수준만
                     감시한다. 비상 임계는 정상 임계의 EMERGENCY_MULT배라는 구조적
                     선택이며, 참고로 2026-08-19의 실측 DD(-9.95%)는 여기 못 미쳐
                     **그 주에는 발동하지 않는다**(지난주 결과에 맞춰 고른 값이 아님을 밝힘).
"""
from __future__ import annotations

from math import sqrt

import pandas as pd

# 프로덕션 상수를 그대로 읽어 온다(값이 바뀌면 섀도우도 같이 따라가야 하므로).
from signal_generator import (  # noqa: E402
    DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD)

SMA_WINDOW = 120

# ── SigmaDDGuard 계수 역산 ────────────────────────────────────
# 20거래일 낙폭은 대략 σ·√20 스케일로 커진다. 그래서 임계를 σ·√lookback 에 비례시키고,
# 비례계수 K 는 "평상시 변동성에서 현행 -8%가 나오도록" 역산한다.
#   NORMAL_SIGMA : 2026-06-25 폭락 시작 이전 250거래일 KODEX200 일간 표준편차(실측 2.82%)
#   K = 0.08 / (0.0282 * √20) ≈ 0.634
# → 평상시엔 현행과 동일하게 동작하고, 변동성이 커진 국면에서만 자동으로 넓어진다.
NORMAL_SIGMA = 0.0282
K_SIGMA = abs(DD_GUARD_THRESHOLD) / (NORMAL_SIGMA * sqrt(DD_GUARD_LOOKBACK))

# ── SigmaDDGuard 재보정 계수 (2026-08-26) ────────────────────
# 왜 두 개인가: 원본 K는 σ를 '250일 창 전체 std'(2.80%)로 재서 역산했는데, 가드가
# 런타임에 쓰는 것은 '20일 롤링 σ'다. 추정량이 달라서 의도했던 "평상시 -8%와 동일"이
# 성립하지 않는다(실 KODEX200 461일 기준 임계 중앙값 -3.92%, 74%의 날에서 고정보다 좁음).
#
# 재보정판은 **창을 바꾸지 않고 추정량만 런타임과 일치**시킨다:
#   창   : 2025-06-17~2026-06-24 (원본과 동일. 폭락 시작 2026-06-25 이전)
#   추정 : 그 창의 20일 롤링 σ 중앙값 = 1.7429%
#   K    = 0.08 / (0.017429 * sqrt(20)) = 1.0264
# 채점 대상 구간(2026-08-03~)은 도출에 쓰이지 않았다 → OOS 유지.
# (전체 481일로 재면 1.38%/K=1.294가 나오지만 채점구간을 포함하므로 인샘플이라 채택 안 함)
#
# 어느 쪽이 옳은지는 고르지 않는다. 두 후보를 원장에 나란히 올려 실측이 판정하게 둔다.
NORMAL_SIGMA_ROLLING = 0.017429
K_SIGMA_RECAL = abs(DD_GUARD_THRESHOLD) / (NORMAL_SIGMA_ROLLING * sqrt(DD_GUARD_LOOKBACK))

# ── DailyCircuitBreaker ──────────────────────────────────────
EMERGENCY_MULT = 2.0          # 비상 임계 = 정상 임계 × 2 (= 현행 기준 -16%)


class GuardVerdict:
    """판정 + 그 판정을 내린 근거.

    왜 필요한가 (gs-quant TriggerInfo 패턴):
      기존 status()는 "BULL"/"BEAR" 문자열만 돌려주고 낙폭·임계·발동조건을 전부 버렸다.
      그래서 원장에는 수익률만 남고, "이 후보가 왜 대피했나"를 물을 때마다
      일회용 스크립트로 과거를 재생해야 했다. 근거를 같이 실어 보내면 원장에 남는다.

    status 만 쓰는 기존 호출부는 무변경으로 동작한다(status() 가 그대로 str 을 반환).
    """

    __slots__ = ("status", "reason")

    def __init__(self, status: str, reason: dict = None):
        if status not in ("BULL", "BEAR"):
            raise ValueError(f"판정은 BULL/BEAR 중 하나여야 한다: {status!r}")
        self.status = status
        self.reason = reason or {}

    @property
    def is_bear(self) -> bool:
        return self.status == "BEAR"

    def __repr__(self) -> str:
        return f"GuardVerdict({self.status!r}, {self.reason!r})"


class MarketGuard:
    """가드 인터페이스. status()는 주간(리밸런싱 시점) 판정, intra_exit()는 구간 중 비상.

    하위클래스는 evaluate()만 구현한다. status()는 거기서 판정만 뽑아내는 얇은 껍데기다.
    """

    name = "guard"

    def evaluate(self, prices, date) -> GuardVerdict:
        raise NotImplementedError

    def status(self, prices, date) -> str:
        return self.evaluate(prices, date).status

    def intra_exit(self, prices, d0, d1):
        """구간 (d0, d1] 중 비상 청산일. 없으면 None (대부분의 가드는 항상 None)."""
        return None

    def describe(self) -> dict:
        return {"name": self.name}

    # ── 공통 유틸 ─────────────────────────────────────────
    @staticmethod
    def _series_upto(prices, date):
        kd = prices.kodex()
        if kd is None:
            return None
        s = kd[kd.index <= pd.Timestamp(date)].dropna()
        return s if len(s) else None

    @classmethod
    def _drawdown(cls, s):
        """20일 고점 대비 낙폭. 표본 부족이면 None."""
        if s is None or len(s) < DD_GUARD_LOOKBACK:
            return None
        return float(s.iloc[-1] / s.tail(DD_GUARD_LOOKBACK).max() - 1)


# ══ 원시 조건 ════════════════════════════════════════════════
# 조합은 아래 AnyOf/AllOf/Not 이 맡는다. 원시 조건은 '하나의 사실'만 판정한다.

class DDGuard(MarketGuard):
    """현행 프로덕션 가드: 20일 고점 대비 -8% 이하면 BEAR."""

    name = "DD가드"

    def __init__(self, threshold: float = DD_GUARD_THRESHOLD):
        self.threshold = threshold

    def evaluate(self, prices, date) -> GuardVerdict:
        dd = self._drawdown(self._series_upto(prices, date))
        bear = dd is not None and dd <= self.threshold
        return GuardVerdict("BEAR" if bear else "BULL",
                            {"rule": self.name, "dd": dd, "threshold": self.threshold,
                             "fired": bear})

    def describe(self) -> dict:
        return {"name": self.name, "threshold_pct": round(self.threshold * 100, 2),
                "lookback": DD_GUARD_LOOKBACK}


class SMABreakGuard(MarketGuard):
    """SMA120 이탈이면 BEAR. 기존 ComboGuard 안에 묻혀 있던 조건을 꺼낸 것."""

    name = "SMA이탈"

    def __init__(self, window: int = SMA_WINDOW):
        self.window = window

    def evaluate(self, prices, date) -> GuardVerdict:
        s = self._series_upto(prices, date)
        if s is None or len(s) < self.window:
            return GuardVerdict("BULL", {"rule": self.name, "fired": False,
                                         "reason": "표본부족"})
        last, sma = float(s.iloc[-1]), float(s.tail(self.window).mean())
        bear = last < sma
        return GuardVerdict("BEAR" if bear else "BULL",
                            {"rule": self.name, "close": last, "sma": sma,
                             "window": self.window, "fired": bear})

    def describe(self) -> dict:
        return {"name": self.name, "sma_window": self.window}


class SigmaDDGuard(MarketGuard):
    """임계를 변동성에 비례시킨 DD가드: 임계 = -K·σ·√lookback.

    ⚠️ 실측 주의 (2026-08-26 확인): NORMAL_SIGMA(2.82%)는 250거래일 창의 σ인데,
       이 가드가 런타임에 쓰는 것은 20일 롤링 σ(실측 중앙값 1.38%)다. 추정량이 서로
       달라서, 의도했던 "평상시엔 현행(-8%)과 동일"이 실제로는 성립하지 않는다.
       실 KODEX200 461일 기준 임계 중앙값은 -3.92%로 현행보다 오히려 2배 민감하고,
       74%의 날에서 고정 임계보다 좁다. K는 동결 원칙에 따라 그대로 두되,
       이 가드를 "평상시 더 민감한 후보"로 읽어야지 "평상시 동일"로 읽으면 안 된다.
    """

    name = "σ임계가드"

    def __init__(self, k: float = K_SIGMA, vol_window: int = DD_GUARD_LOOKBACK):
        self.k = k
        self.vol_window = vol_window

    def threshold_at(self, prices, date):
        """그 시점의 동적 임계(음수). 표본 부족이면 None → 판정 불가."""
        s = self._series_upto(prices, date)
        if s is None or len(s) < self.vol_window + 1:
            return None
        sigma = float(s.pct_change().dropna().tail(self.vol_window).std())
        if not sigma or sigma <= 0:
            return None
        return -self.k * sigma * sqrt(DD_GUARD_LOOKBACK)

    def evaluate(self, prices, date) -> GuardVerdict:
        dd = self._drawdown(self._series_upto(prices, date))
        th = self.threshold_at(prices, date)
        if dd is None or th is None:
            # 판정 불가 시 프로덕션과 같은 fail 방향(고정 임계로 폴백)을 쓴다.
            v = DDGuard().evaluate(prices, date)
            return GuardVerdict(v.status, {"rule": self.name, "fallback": "DD가드",
                                           **v.reason})
        bear = dd <= th
        return GuardVerdict("BEAR" if bear else "BULL",
                            {"rule": self.name, "dd": dd, "threshold": th,
                             "vol_window": self.vol_window, "fired": bear})

    def describe(self) -> dict:
        # 표본길이(vol_window)와 낙폭 지평(DD_GUARD_LOOKBACK)은 서로 다른 개념이다.
        # 고정 문자열로 쓰면 vol_window를 바꿔 스윕할 때 원장에 거짓 파라미터가 박힌다.
        return {"name": self.name, "k": round(self.k, 4),
                "formula": (f"threshold = -K * sigma{self.vol_window} "
                            f"* sqrt({DD_GUARD_LOOKBACK})"),
                "k_derivation": f"|{DD_GUARD_THRESHOLD}| / ({NORMAL_SIGMA} * sqrt({DD_GUARD_LOOKBACK}))",
                "k_caveat": "NORMAL_SIGMA는 250일 창 추정, 런타임 σ는 20일 롤링 — 추정량 불일치",
                "k_variant": ("recalibrated(20일 롤링 중앙값 기준)"
                              if abs(self.k - K_SIGMA_RECAL) < 1e-9
                              else "original(250일 창 std 기준)"),
                "normal_sigma": NORMAL_SIGMA, "vol_window": self.vol_window}


# ══ 조합자 ═══════════════════════════════════════════════════
# gs-quant 의 AggregateTriggerRequirements(ALL_OF/ANY_OF) / NotTriggerRequirements 대응.
# 후보를 늘릴 때 클래스를 새로 만들 필요가 없어진다.

class _Composite(MarketGuard):
    def __init__(self, guards, name: str = None):
        self.guards = list(guards)
        if not self.guards:
            raise ValueError("조합자는 가드를 최소 1개 받아야 한다")
        if name:
            self.name = name

    def describe(self) -> dict:
        return {"name": self.name, "op": self.OP,
                "members": [g.describe() for g in self.guards]}


class AnyOf(_Composite):
    """하나라도 BEAR면 BEAR (OR). 단락하지 않고 전부 평가해 근거를 모은다."""

    OP = "ANY_OF"
    name = "AnyOf"

    def evaluate(self, prices, date) -> GuardVerdict:
        vs = [g.evaluate(prices, date) for g in self.guards]
        fired = [v.reason for v in vs if v.is_bear]
        return GuardVerdict("BEAR" if fired else "BULL",
                            {"rule": self.name, "op": self.OP,
                             "fired_by": fired,
                             "members": [v.reason for v in vs]})


class AllOf(_Composite):
    """전부 BEAR여야 BEAR (AND)."""

    OP = "ALL_OF"
    name = "AllOf"

    def evaluate(self, prices, date) -> GuardVerdict:
        vs = [g.evaluate(prices, date) for g in self.guards]
        bear = all(v.is_bear for v in vs)
        return GuardVerdict("BEAR" if bear else "BULL",
                            {"rule": self.name, "op": self.OP,
                             "members": [v.reason for v in vs]})


class Not(MarketGuard):
    """판정 반전 (NOT)."""

    name = "Not"

    def __init__(self, guard: MarketGuard):
        self.guard = guard

    def evaluate(self, prices, date) -> GuardVerdict:
        v = self.guard.evaluate(prices, date)
        return GuardVerdict("BULL" if v.is_bear else "BEAR",
                            {"rule": self.name, "op": "NOT", "inner": v.reason})

    def describe(self) -> dict:
        return {"name": self.name, "op": "NOT", "inner": self.guard.describe()}


class ComboGuard(AnyOf):
    """DD가드 OR SMA120 이탈. 기존 `use_sma=True` 후보와 동일 동작.

    이제 상속이 아니라 조합으로 표현된다 — 같은 걸 원시 조건 두 개의 AnyOf로 쓸 수 있다.
    """

    name = "콤보가드"

    def __init__(self, threshold: float = DD_GUARD_THRESHOLD, sma_window: int = SMA_WINDOW):
        super().__init__([DDGuard(threshold), SMABreakGuard(sma_window)])
        self.threshold = threshold

    def describe(self) -> dict:
        return {"name": self.name, "threshold_pct": round(self.threshold * 100, 2),
                "lookback": DD_GUARD_LOOKBACK, "sma_window": SMA_WINDOW,
                "op": self.OP, "members": [g.describe() for g in self.guards]}


class DailyCircuitBreaker(MarketGuard):
    """주간 판정은 base 그대로 + 구간 중 매일 '비상' 수준이면 그날 전량 청산.

    사용자 제안(2026-08-23): "하루 한 번 조회해서 비상인지 확인하고, 아니면 패스,
    매수는 월요일만". 재진입을 다음 주간 리밸런싱까지 미루는 비대칭이 핵심이라
    그대로 모형화한다. 비상 임계를 정상 임계와 같게 두면(-8%) 평균회귀 국면에서
    반등 전날마다 파는 장치가 되므로, 정상 임계의 EMERGENCY_MULT배로 벌린다.
    """

    name = "일간비상차단기"

    def __init__(self, base: MarketGuard = None, mult: float = EMERGENCY_MULT):
        self.base = base or DDGuard()
        self.mult = mult
        self.emergency_threshold = DD_GUARD_THRESHOLD * mult

    def evaluate(self, prices, date) -> GuardVerdict:
        v = self.base.evaluate(prices, date)
        return GuardVerdict(v.status, {"rule": self.name, "delegated_to": self.base.name,
                                       "base": v.reason})

    def intra_exit(self, prices, d0, d1):
        """(d0, d1] 구간에서 비상 임계를 처음 밑도는 날. 없으면 None."""
        kd = prices.kodex()
        if kd is None:
            return None
        s = kd.dropna()
        window = s[(s.index > pd.Timestamp(d0)) & (s.index <= pd.Timestamp(d1))]
        for ts in window.index:
            dd = self._drawdown(s[s.index <= ts])
            if dd is not None and dd <= self.emergency_threshold:
                return ts.to_pydatetime()
        return None

    def describe(self) -> dict:
        return {"name": self.name, "base": self.base.describe(),
                "emergency_threshold_pct": round(self.emergency_threshold * 100, 2),
                "emergency_mult": self.mult,
                "reentry": "다음 주간 리밸런싱까지 현금 유지(비대칭)"}
