"""
s3_keys.py — S3 아카이브 키 결정 (테스트 실행이 실이력 오염 방지)
========================================================
force_bull=True는 재진입 검증용 테스트 실행이다. 이게 실제 날짜별 아카이브
(quant_signals/·universe/)를 덮어쓰면 주간 성과 분석·섀도우 데이터가 오염된다
(실사고: 2026-07-24 아카이브가 force_bull 테스트로 덮여 market_status 가짜 BULL).
→ 테스트 실행은 별도 *_test/ prefix로 격리한다. 순수 함수(테스트 용이).

주의: Lambda B가 읽는 최신 시그널(QUANT_SIGNAL_KEY)은 여기서 다루지 않는다.
      force_bull 테스트는 의도적으로 Lambda B를 구동하므로 최신 키는 그대로 쓴다.
"""
from __future__ import annotations


def archive_keys(date_str: str, force_bull: bool) -> tuple:
    """(quant_signals 아카이브 키, universe 스냅샷 키) 반환.

    force_bull(테스트)이면 *_test/ prefix로 실이력과 분리.
    """
    if force_bull:
        return f"quant_signals_test/{date_str}.json", f"universe_test/{date_str}.json"
    return f"quant_signals/{date_str}.json", f"universe/{date_str}.json"
