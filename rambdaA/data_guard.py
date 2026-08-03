"""
data_guard.py — 외부 시세 데이터 신선도·정합성 검증 (야후 단일의존 실패 방지)
========================================================
야후(yf.py)가 낡은/빈/비정상 값을 반환해도 그걸로 매매 판단하지 않도록,
가드/모멘텀이 쓰는 가격 데이터를 사용 전에 검증한다. 순수 함수(무거운 의존 없음)라
단위테스트가 쉽다. 판단은 호출부(signal_generator)가 하고, 여기선 검사만.

설계: 실패 시 fail-safe — 검증 실패면 호출부는 그 데이터로 계산하지 않는다
      (가드는 '판단 불가'로 건너뛰고, 낡은 값으로 잘못된 BULL/BEAR를 내지 않음).
"""
from __future__ import annotations

from datetime import datetime


def validate_prices(last_date, num_rows: int, last_value,
                    as_of: datetime, min_rows: int, max_stale_days: int = 5) -> tuple:
    """가격 시계열 검증. (ok: bool, reason: str) 반환.

    - num_rows < min_rows        → 데이터 부족(모멘텀/가드 계산 불가)
    - last_value None/0/음수     → 비정상 값(야후 결측/오류)
    - last_date가 as_of보다 미래 → 시계 오류(미래참조)
    - (as_of - last_date) 초과   → 낡은 데이터(장 지연/피드 중단)
    """
    if num_rows is None or num_rows < min_rows:
        return False, f"데이터 부족: {num_rows} < {min_rows}행"
    if last_value is None or last_value <= 0:
        return False, f"최신가 비정상: {last_value}"
    if last_date is None:
        return False, "최신 날짜 없음"
    stale = (as_of - last_date).days
    if stale < 0:
        return False, f"미래 데이터(시계 오류): {stale}일"
    if stale > max_stale_days:
        return False, f"데이터 낡음: {stale}일 지연(> {max_stale_days})"
    return True, "ok"
