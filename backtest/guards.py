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

# ── DailyCircuitBreaker ──────────────────────────────────────
EMERGENCY_MULT = 2.0          # 비상 임계 = 정상 임계 × 2 (= 현행 기준 -16%)


class MarketGuard:
    """가드 인터페이스. status()는 주간(리밸런싱 시점) 판정, intra_exit()는 구간 중 비상."""

    name = "guard"

    def status(self, prices, date) -> str:
        raise NotImplementedError

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


class DDGuard(MarketGuard):
    """현행 프로덕션 가드: 20일 고점 대비 -8% 이하면 BEAR."""

    name = "DD가드"

    def __init__(self, threshold: float = DD_GUARD_THRESHOLD):
        self.threshold = threshold

    def status(self, prices, date) -> str:
        dd = self._drawdown(self._series_upto(prices, date))
        return "BEAR" if (dd is not None and dd <= self.threshold) else "BULL"

    def describe(self) -> dict:
        return {"name": self.name, "threshold_pct": round(self.threshold * 100, 2),
                "lookback": DD_GUARD_LOOKBACK}


class ComboGuard(DDGuard):
    """DD가드 OR SMA120 이탈. 기존 `use_sma=True` 후보와 동일 동작."""

    name = "콤보가드"

    def status(self, prices, date) -> str:
        s = self._series_upto(prices, date)
        if super().status(prices, date) == "BEAR":
            return "BEAR"
        if s is not None and len(s) >= SMA_WINDOW and \
                float(s.iloc[-1]) < float(s.tail(SMA_WINDOW).mean()):
            return "BEAR"
        return "BULL"

    def describe(self) -> dict:
        d = super().describe()
        d.update({"name": self.name, "sma_window": SMA_WINDOW})
        return d


class SigmaDDGuard(MarketGuard):
    """임계를 변동성에 비례시킨 DD가드: 임계 = -K·σ·√lookback.

    고정 -8%는 평상시(σ=2.8%)엔 2.8σ짜리 '진짜 폭락 감지기'지만, 변동성이 3배가 된
    국면에선 1.3σ — 이틀치 노이즈에 켜진다. 이 후보는 그 눈금을 국면에 맞춘다.
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

    def status(self, prices, date) -> str:
        s = self._series_upto(prices, date)
        dd = self._drawdown(s)
        th = self.threshold_at(prices, date)
        if dd is None or th is None:
            # 판정 불가 시 프로덕션과 같은 fail 방향(고정 임계로 폴백)을 쓴다.
            return DDGuard().status(prices, date)
        return "BEAR" if dd <= th else "BULL"

    def describe(self) -> dict:
        return {"name": self.name, "k": round(self.k, 4),
                "formula": "threshold = -K * sigma20 * sqrt(20)",
                "k_derivation": f"|{DD_GUARD_THRESHOLD}| / ({NORMAL_SIGMA} * sqrt({DD_GUARD_LOOKBACK}))",
                "normal_sigma": NORMAL_SIGMA, "vol_window": self.vol_window}


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

    def status(self, prices, date) -> str:
        return self.base.status(prices, date)

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
