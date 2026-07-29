"""
strategies/leverage.py — 공격형 후보 #3: 레버리지형(2X Momentum)
========================================================
가장 공격적인 후보. 평소 유니버스에서 제외하던 2배 레버리지 ETF를 별도 유니버스
("leverage")에서 골라 모멘텀 상위에 집중한다. 상승장 수익을 극대화하되,
하락장 방어(VIX+DD 가드)는 그대로 유지한다 — 가드 없는 레버리지는 자살행위.

정의:
  - universe_tag: "leverage" → 백테스트가 국내 2X ETF만으로 유니버스를 구성
  - 126일 모멘텀 상위 5종목(레버리지는 섹터 개념이 무의미해 섹터캡 미적용)
  - 균등가중, BEAR 전량현금(가드 유지)

⚠️ 성격: 변동성 2배 + 지수형 상품의 감쇠(decay) 리스크. 최고 수익/최고 위험.
   반드시 섀도우로만 검증하고, 가드 동작을 최우선으로 확인한다.
"""
from __future__ import annotations

from base import Strategy


class LeverageMomentum2X(Strategy):
    name = "aggressive_leverage_2x"
    description = "공격형(레버리지): 2X ETF·126일 모멘텀·top5·균등·BEAR 전량현금"
    lookback = 126
    universe_tag = "leverage"

    def __init__(self, n: int = 5):
        self.n = n

    def select(self, universe: list[dict], market: dict) -> list[dict]:
        # 가드는 반드시 존중 — 레버리지는 하락장 노출이 치명적
        if market.get("market_status") == "BEAR":
            return []
        scored = sorted(universe, key=lambda x: x["momentum"], reverse=True)
        return scored[:self.n]  # 레버리지는 섹터캡 대신 순수 모멘텀 상위 N
