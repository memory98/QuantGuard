#!/usr/bin/env python3
"""
backtest/validate_combo.py — 콤보 가드(SMA120 OR DD-8%) 실제 포트폴리오 검증
========================================================
guard_mechanisms에서 콤보가 '지수 격리' 기준으론 가장 robust했다. 여기서는 그것을
**실제 4전략 포트폴리오**(longrun)에 얹어 현행 가드(VIX+DD)와 나란히 비교한다.

⚠️ 데이터 한계(정직히): longrun은 yf.py(2y 하드캡)를 쓰므로 검증 창이 최근 ~1년
   (2025-05~2026-07)뿐이다. 콤보의 진짜 이점(2018/2022 완만한 하락)은 이 창 밖이라
   여기서는 '콤보가 최근 급락장에서 현행 대비 손해를 끼치지 않는가'까지만 확인 가능하다.
   완만한 하락 국면 이점은 guard_mechanisms(10년 지수)에서 별도 입증된 것.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))

from longrun import (_build_data, LongBacktest, GuardSimulator, benchmark)  # noqa: E402
from baseline import BaselineMomentum126      # noqa: E402
from aggressive import ConcentratedMomentum   # noqa: E402
from fast import FastMomentum63               # noqa: E402
from leverage import LeverageMomentum2X       # noqa: E402


def _strats():
    return [BaselineMomentum126(), ConcentratedMomentum(),
            FastMomentum63(), LeverageMomentum2X()]


def _run(universes, prices, guard):
    return LongBacktest(universes, prices, guard, _strats()).run()


def main():
    universes, prices = _build_data(80)
    cur = _run(universes, prices, GuardSimulator(prices))                    # 현행: VIX+DD
    combo = _run(universes, prices, GuardSimulator(prices, sma_window=120))  # 콤보: +SMA120 OR

    fr = LongBacktest(universes, prices, GuardSimulator(prices), _strats())._fridays()
    bench = benchmark(prices, fr[0], fr[-1])
    print(f"\n🗓  검증 구간: {fr[0]:%Y-%m-%d} ~ {fr[-1]:%Y-%m-%d} ({len(fr)-1}주)")
    print("   (⚠️ 최근 ~1년 = 강세장+2026 급락만. 완만한 하락 이점은 이 창 밖 → guard_mechanisms 참조)")

    print(f"\n{'전략':<30}{'현행 net':>10}{'현MDD':>8} │{'콤보 net':>10}{'콤MDD':>8}   {'ΔMDD':>7}")
    for name in cur:
        c, k = cur[name], combo[name]
        d_mdd = k["mdd_pct"] - c["mdd_pct"]
        print(f"{name:<30}{c['total_net_pct']:>+9.1f}%{c['mdd_pct']:>+7.1f}% │"
              f"{k['total_net_pct']:>+9.1f}%{k['mdd_pct']:>+7.1f}%   {d_mdd:>+6.1f}%p")
    print(f"{'[벤치] KODEX200 매수보유':<30}{bench['total_net_pct']:>+9.1f}%{bench['mdd_pct']:>+7.1f}%")
    print("\n판독: ΔMDD가 음수면 콤보가 낙폭을 더 줄인 것. 수익이 크게 안 깎이면서 MDD가 개선/유지면 통과.")


if __name__ == "__main__":
    main()
