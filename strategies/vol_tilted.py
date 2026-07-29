"""
strategies/vol_tilted.py — 공격형 재설계: 리스크조정 선별 + 공격적 가중
========================================================
순수 변동성조정(vol_adjusted)은 강세장에서 상승을 너무 포기했다(방어형 틸트).
이 버전은 '똑똑하게 공격적'을 노린다:
  - 선별(랭킹): 모멘텀 / 변동성 → 폭락 위험이 큰 고변동성 반짝급등주를 걸러냄
  - 집중/가중: top5 + 모멘텀 비례가중 → 상승 여력은 집중형처럼 유지
공통: 섹터당 1개, BEAR 전량현금

가설: '집중형'의 공격성을 유지하되, 되돌림 위험이 큰 종목만 선별 단계에서 회피 →
      집중형 대비 낙폭을 낮추면서 수익은 크게 안 깎기. 백테스트가 아니라 섀도우 OOS로 판정.
"""
from __future__ import annotations

from base import Strategy, sector_capped


class VolTiltedConcentrated(Strategy):
    name = "aggressive_voltilt_top5"
    description = "공격형(리스크조정 선별+집중): 모멘텀/변동성 랭킹·top5·모멘텀가중·섹터당1·BEAR현금"
    lookback = 126
    universe_tag = "normal"

    def __init__(self, n: int = 5):
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
        # 모멘텀 비례가중(집중형과 동일) — 상승 여력 유지
        if not picks:
            return {}
        raw = [max(p["momentum"], 0.0) for p in picks]
        total = sum(raw)
        if total <= 0:
            w = 1.0 / len(picks)
            return {p["code"]: w for p in picks}
        return {p["code"]: r / total for p, r in zip(picks, raw)}
