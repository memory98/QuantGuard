# usa.py — Lambda B: 국외 ETF 매매 로직 (확장 뼈대)
from config import BUDGET_RATIO


def run_usa_rebalancing(
    token: str,
    real_total_equity: int,
    is_test: bool,
    execute_order_fn,
) -> dict:
    """
    미국 ETF 리밸런싱 실행 함수

    BUDGET_RATIO = 1.0 → 국내 전액 투입 중, 미국 패스
    BUDGET_RATIO < 1.0 → (1.0 - BUDGET_RATIO) 비중만큼 미국 ETF 운용
    """
    usa_budget_ratio = round(1.0 - BUDGET_RATIO, 4)

    if usa_budget_ratio <= 0:
        print(
            f"🇺🇸 [USA 패스] BUDGET_RATIO={BUDGET_RATIO} → "
            f"국내주식에 {BUDGET_RATIO*100:.0f}% 전액 투입 중. "
            f"미국 ETF 비중 없음 → 이번 주 미국 매매 건너뜁니다."
        )
        return {"result": "SKIPPED", "reason": f"BUDGET_RATIO={BUDGET_RATIO}"}

    usa_budget = real_total_equity * usa_budget_ratio
    print(f"🇺🇸 [USA 가동] 미국 ETF 운용 예산: {usa_budget:,.0f}원 ({usa_budget_ratio*100:.0f}%)")

    # ── 미국 ETF 매매 로직 (BUDGET_RATIO < 1.0 시 구현 예정) ─
    # 예: TIGER 미국S&P500(360750) 등 국내 상장 해외 ETF
    # execute_order_fn(token, "360750", target_qty, is_buy=True)
    print("🚧 [USA] 미국 ETF 매매 로직 미구현 — 향후 확장 예정")
    return {"result": "NOT_IMPLEMENTED"}
