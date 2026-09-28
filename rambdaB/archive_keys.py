"""
archive_keys.py — S3 기록 키 결정 (테스트 실행이 실전 이력·중복 가드를 오염하는 것 방지)
========================================================
[fix42 / #OPEN-ISO] rambdaB는 FORCE_TEST_MODE와 무관하게 실전 키(latest_signal.json,
latest_signal/<날짜>.json)에 결과를 써 왔다. 그 결과 **테스트 실행이 실전 실행을
취소시킬 수 있었다**:

  ① 리밸런싱일(월) 14:20 이전에 콘솔 테스트(FORCE_TEST_MODE=True + force_run) 실행
  ② 그날짜 '실전' 아카이브가 생성됨
  ③ 14:20 정기 실행이 fix15 중복 실행 가드(head_object)에 걸려 **차단**
  ④ 그 주가 BEAR 전환 주면 DD가드 대피 매도가 실행되지 않는다(손실 방향)

rambdaA는 fix22에서 같은 문제를 이미 격리했다(quant_signals_test/·universe_test/,
2026-07-24 실사고 대응). 이 모듈은 그 장치를 rambdaB에 옮긴 것이다. 순수 함수(테스트 용이).

이름이 rambdaA의 `s3_keys.py`와 다른 이유: 테스트는 한 프로세스에서 rambdaA/rambdaB를
모두 import하는데, 같은 모듈명이 둘이면 먼저 로드된 쪽이 sys.modules를 점유해 다른 쪽
테스트가 조용히 엉뚱한 모듈을 검증하게 된다(실제로 그렇게 깨졌다). 이름을 분리해 차단한다.

주의: **읽기 키는 격리하지 않는다.** 테스트 실행도 직전 실전 스냅샷(prev_equity)을
      그대로 읽어야 현실적인 검증이 된다. 격리 대상은 '쓰기'뿐이다.
"""
from __future__ import annotations

LIVE_LATEST_KEY = "latest_signal.json"
TEST_LATEST_KEY = "latest_signal_test.json"
LIVE_ARCHIVE_PREFIX = "latest_signal"
TEST_ARCHIVE_PREFIX = "latest_signal_test"


def archive_keys(date_str: str, is_test: bool) -> tuple:
    """(최신본 키, 날짜별 아카이브 키) 반환. 테스트면 *_test 로 실전과 분리.

    date_str: KST 기준 'YYYY-MM-DD'
    is_test:  FORCE_TEST_MODE
    """
    if is_test:
        return TEST_LATEST_KEY, f"{TEST_ARCHIVE_PREFIX}/{date_str}.json"
    return LIVE_LATEST_KEY, f"{LIVE_ARCHIVE_PREFIX}/{date_str}.json"


def live_archive_key(date_str: str) -> str:
    """중복 실행 가드가 조회할 키 — 항상 '실전' 아카이브다.

    '오늘 실매매가 이미 돌았는가'를 판별하는 것이 가드의 목적이므로,
    테스트 모드로 실행 중이더라도 실전 키를 본다(테스트 기록은 차단 근거가 아니다).
    """
    return archive_keys(date_str, is_test=False)[1]
