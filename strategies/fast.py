"""
strategies/fast.py — 공격형 후보 #2: 속도형(Fast Momentum 63d)
========================================================
공격성 레버를 '신호 속도' 하나로 격리. baseline과 유니버스·종목수·가중·가드는
같고, 모멘텀 룩백만 126일→63일로 단축해 추세 전환에 더 빨리 반응한다.

baseline 대비 차이:
  - lookback: 126 → 63 (백테스트가 이 값으로 momentum을 채점)
공통(유지):
  - 섹터당 1개, top10, 균등가중, BEAR 전량현금

성격: 추세 전환에 민첩 ↔ 턴오버·휘프소 비용 증가. baseline과 나란히 두면
      '느린 신호 vs 빠른 신호'의 순수 효과를 비교할 수 있다.
"""
from __future__ import annotations

from base import Strategy, sector_capped


class FastMomentum63(Strategy):
    name = "aggressive_fast_63"
    description = "공격형(속도): 63일 모멘텀·섹터당 1개·top10·균등·BEAR 전량현금"
    lookback = 63
    universe_tag = "normal"

    def __init__(self, n: int = 10):
        self.n = n

    def select(self, universe: list[dict], market: dict) -> list[dict]:
        if market.get("market_status") == "BEAR":
            return []
        scored = sorted(universe, key=lambda x: x["momentum"], reverse=True)
        return sector_capped(scored, self.n, per_sector=1)
