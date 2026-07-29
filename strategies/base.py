"""
strategies/base.py — 섀도우 전략 공통 인터페이스
========================================================
목적: 실전(rambdaA)과 무관한 '종이 전략'을 여러 개 정의하고,
      동일한 유니버스 스냅샷 위에서 서로 비교(백테스트/전진검증)하기 위한 골격.

원칙 (CLAUDE.md):
  - 객체지향: 각 공식은 Strategy 하위클래스 하나로 표현한다.
  - 읽기 전용: 이 패키지는 주문 집행(korea.py)에 절대 연결하지 않는다.
  - 단일 입력 계약: select()는 '채점된 유니버스 리스트'만 받는다.
      universe item 스키마(= signal_generator가 저장하는 scored item):
        {"code","name","price","momentum","sector","base_date","base_price"}
      market: {"market_status": "BULL"|"BEAR"|"UNKNOWN", "vix": float|None,
               "domestic_dd": float|None}
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Strategy(ABC):
    """모든 섀도우 전략의 부모. select()만 구현하면 균등가중 포트폴리오가 나온다."""

    name: str = "base"
    description: str = ""
    # 백테스트가 이 전략에 유니버스를 어떻게 만들어 줄지 선언한다.
    #   lookback: 모멘텀 계산 기간(영업일). 백테스트가 이 값으로 momentum을 채점한다.
    #   universe_tag: 어느 유니버스에서 고를지 ("normal"=국내 일반 ETF, "leverage"=2X ETF)
    lookback: int = 126
    universe_tag: str = "normal"

    @abstractmethod
    def select(self, universe: list[dict], market: dict) -> list[dict]:
        """유니버스에서 매수 대상을 골라 '순위 순서'대로 반환한다(부분집합).

        하락장 대피 로직도 여기서 표현한다(대피면 빈 리스트 반환).
        """
        raise NotImplementedError

    def weights(self, picks: list[dict]) -> dict[str, float]:
        """기본은 균등가중. 모멘텀 가중 등은 하위클래스에서 override."""
        if not picks:
            return {}
        w = 1.0 / len(picks)
        return {p["code"]: w for p in picks}

    def build_portfolio(self, universe: list[dict], market: dict) -> dict:
        """select + weights를 묶은 최종 포트폴리오."""
        picks = self.select(universe, market)
        return {
            "strategy": self.name,
            "picks": picks,
            "weights": self.weights(picks),
            "cash": len(picks) == 0,
        }


def sector_capped(scored: list[dict], n: int, per_sector: int = 1) -> list[dict]:
    """모멘텀 내림차순으로 순회하며 섹터당 per_sector개까지, 최대 n개 선택.

    signal_generator.apply_sector_filter(per_sector=1)와 동일 결과를 내도록 설계했다.
    (heavy import 회피 목적의 로컬 재구현 — 향후 parity 테스트로 동치성 고정 예정)
    """
    from collections import defaultdict

    seen: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    for s in scored:
        if len(out) >= n:
            break
        sec = s["sector"]
        if seen[sec] < per_sector:
            seen[sec] += 1
            out.append(s)
    return out
