"""
strategies/aggressive.py — 공격형 후보 #1: 집중형(Concentrated Momentum)
========================================================
설계 의도: '공격성' 레버를 '집중도' 하나로만 격리해, 현행(baseline) 대비
          순수하게 집중의 효과/비용만 측정한다. 스냅샷 스키마 변경 없이
          현재 유니버스 데이터로 즉시 백테스트 가능.

baseline 대비 차이:
  - 종목 수: 10 → 5 (top5 압축)
  - 가중: 균등 → 모멘텀 비례가중
공통(유지):
  - 섹터당 1개
  - market_status == BEAR 이면 전량 현금(DD가드 존중)

가중 규칙(견고성):
  - 음수 모멘텀은 0으로 클립(래거드에 비중 주지 않음)
  - 클립 후 합이 0이면 균등가중으로 폴백(전부 비양수인 방어적 상황)
"""
from __future__ import annotations

from base import Strategy, sector_capped


class ConcentratedMomentum(Strategy):
    name = "aggressive_concentrated_top5"
    description = "공격형: 126일 모멘텀·섹터당 1개·top5·모멘텀비례가중·BEAR 전량현금"

    def __init__(self, n: int = 5):
        self.n = n

    def select(self, universe: list[dict], market: dict) -> list[dict]:
        if market.get("market_status") == "BEAR":
            return []
        scored = sorted(universe, key=lambda x: x["momentum"], reverse=True)
        return sector_capped(scored, self.n, per_sector=1)

    def weights(self, picks: list[dict]) -> dict[str, float]:
        if not picks:
            return {}
        raw = [max(p["momentum"], 0.0) for p in picks]
        total = sum(raw)
        if total <= 0:  # 방어적 상황: 전부 비양수 → 균등가중 폴백
            w = 1.0 / len(picks)
            return {p["code"]: w for p in picks}
        return {p["code"]: r / total for p, r in zip(picks, raw)}
