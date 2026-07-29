"""
strategies/vol_adjusted.py — 공격형 개선안: 변동성조정 모멘텀(Risk-adjusted)
========================================================
'더 똑똑한 공격'. 순수 모멘텀은 고변동성 종목의 반짝 급등에 쏠려 되돌림에 취약하다.
여기서는:
  - 랭킹: 모멘텀 / 변동성 (리스크 대비 수익, Sharpe형 점수) — 꾸준한 상승 선호
  - 가중: 역변동성(1/vol) — 안정적 종목에 더, 변동성 큰 종목에 덜 (리스크 패리티형)
공통(유지): 섹터당 1개, top10, BEAR 전량현금

기대 효과: 블로업(폭락 종목) 회피, 리스크 대비 수익↑. 절대수익은 집중형보다 낮을 수
          있으나 낙폭이 얕아지는 게 목표. 백테스트가 아니라 섀도우 OOS로 최종 판정한다.

필요 데이터: 유니버스 item에 "vol"(일간수익률 표준편차) 필드. 백테스트/파이프라인이
            가격에서 계산해 넣어준다. 없으면 순수 모멘텀으로 안전 폴백.
"""
from __future__ import annotations

from base import Strategy, sector_capped


class VolAdjustedMomentum(Strategy):
    name = "aggressive_vol_adjusted"
    description = "공격형(변동성조정): 모멘텀/변동성 랭킹·역변동성 가중·섹터당1·top10·BEAR현금"
    lookback = 126
    universe_tag = "normal"
    vol_window = 63

    def __init__(self, n: int = 10):
        self.n = n

    @staticmethod
    def _score(x: dict) -> float:
        v = x.get("vol")
        return (x["momentum"] / v) if (v and v > 0) else x["momentum"]

    def select(self, universe: list[dict], market: dict) -> list[dict]:
        if market.get("market_status") == "BEAR":
            return []
        ranked = sorted(universe, key=self._score, reverse=True)
        return sector_capped(ranked, self.n, per_sector=1)

    def weights(self, picks: list[dict]) -> dict[str, float]:
        if not picks:
            return {}
        inv = [(1.0 / p["vol"]) if p.get("vol") and p["vol"] > 0 else 1.0 for p in picks]
        total = sum(inv)
        if total <= 0:
            w = 1.0 / len(picks)
            return {p["code"]: w for p in picks}
        return {p["code"]: iv / total for p, iv in zip(picks, inv)}
