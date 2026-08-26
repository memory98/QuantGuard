"""
backtest/costs.py — 거래비용 단일 소스
========================================================
왜 만들었나:
  side당 0.2%라는 같은 가정이 shadow_forward / runner / longrun 세 곳에 각각
  독립 리터럴로 박혀 있었다. 한 곳만 바꾸면 원장끼리 조용히 비교 불가능해진다.
  (gs-quant는 Action마다 transaction_cost / transaction_cost_exit 를 따로 들고 있다.
   진입과 청산의 비용이 다를 수 있다는 가정 자체를 표현할 수 있어야 한다.)

기본값은 기존 세 파일과 동일한 대칭 0.2%다. 즉 이 모듈 도입만으로는
어떤 수치도 바뀌지 않는다(등가성은 테스트로 고정).
"""
from __future__ import annotations

DEFAULT_PER_SIDE = 0.002      # side당 0.2% → 왕복 0.4% (기존 세 파일의 공통 가정)


class CostModel:
    """진입/청산 비용을 따로 들 수 있는 거래비용 모형.

    entry/exit 를 다르게 주면 비대칭 비용이 되고, 같게 주면 기존과 완전히 동일하다.
    """

    def __init__(self, entry: float = DEFAULT_PER_SIDE, exit: float = DEFAULT_PER_SIDE):
        if entry < 0 or exit < 0:
            raise ValueError(f"거래비용은 음수일 수 없다: entry={entry}, exit={exit}")
        self.entry = entry
        self.exit = exit

    @property
    def is_symmetric(self) -> bool:
        return self.entry == self.exit

    @property
    def round_trip(self) -> float:
        return self.entry + self.exit

    def on_turnover(self, buy_turnover: float, sell_turnover: float) -> float:
        """매수/매도 회전량에 각각의 비용률을 물린 총비용.

        대칭(entry==exit)이면 (buy+sell)*per_side 와 정확히 같다 —
        기존 `turnover * COST_PER_SIDE` 계산과 등가.
        """
        return buy_turnover * self.entry + sell_turnover * self.exit

    def describe(self) -> dict:
        return {"entry_pct": round(self.entry * 100, 4),
                "exit_pct": round(self.exit * 100, 4),
                "round_trip_pct": round(self.round_trip * 100, 4),
                "symmetric": self.is_symmetric}

    def __repr__(self) -> str:
        return f"CostModel(entry={self.entry}, exit={self.exit})"


DEFAULT_COST = CostModel()


def split_turnover(new_w: dict, prev_w: dict) -> tuple[float, float]:
    """비중 변화를 매수 회전 / 매도 회전으로 분해한다.

    합(buy+sell)은 기존 `sum(|Δw|)` 와 항상 같다(분해일 뿐 총량 불변).
    """
    buy = sell = 0.0
    for c in set(new_w) | set(prev_w):
        d = new_w.get(c, 0.0) - prev_w.get(c, 0.0)
        if d > 0:
            buy += d
        else:
            sell += -d
    return buy, sell
