"""
strategies/baseline.py — 현행 운영 전략의 종이 재현(비교 기준선)
========================================================
섀도우 전략들의 성과를 '지금 실전과 대비' 하려면 현행 공식을 동일 인터페이스로
재현해 둔 기준선이 필요하다. 이 클래스가 그 기준선이다.

현행 공식(signal_generator.py 기준):
  - 126일 모멘텀 내림차순
  - 섹터당 1개, 최대 10종목
  - 균등가중
  - market_status == BEAR 이면 전량 현금(빈 포트폴리오)
"""
from __future__ import annotations

from base import Strategy, sector_capped


class BaselineMomentum126(Strategy):
    name = "baseline_momentum_126"
    description = "현행 운영 전략: 126일 모멘텀·섹터당 1개·top10 균등·BEAR 전량현금"

    def __init__(self, n: int = 10):
        self.n = n

    def select(self, universe: list[dict], market: dict) -> list[dict]:
        # 하락장 대피는 실전과 동일하게 존중(현금)
        if market.get("market_status") == "BEAR":
            return []
        scored = sorted(universe, key=lambda x: x["momentum"], reverse=True)
        return sector_capped(scored, self.n, per_sector=1)
