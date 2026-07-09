# config.py — 글로벌 세팅 값 전용 (rambdaB)
# 버전: v1.0.20260709.1
import os

# ── S3 ──────────────────────────────────────────────────────
S3_BUCKET_NAME   = "eunsung-quant-guard-bucket"
SIGNAL_FILE_KEY  = "latest_signal.json"
QUANT_SIGNAL_KEY = "quant_signals.json"

# ── 운용 파라미터 ────────────────────────────────────────────
NUM_TARGETS      = 10   # 최종 매수 종목 수
EXIT_RANK_BUFFER = 15   # [fix16] 순위 히스테리시스: 보유 종목은 이 순위 밖으로 밀려야 매도
BUDGET_RATIO     = 1.0  # 국내주식 투자 비율 (1.0=100%, 0.5=50%)

VIX_TICKER      = "^VIX"
VIX_THRESHOLD   = 30.0   # BEAR 전환 임계값
BEAR_LIMIT_RATE = -0.01  # BEAR 지정가 매도 호가: 현재가 대비 -1%

# ── [fix15] 주문 미세조정 파라미터 ──────────────────────────
MIN_ORDER_VALUE           = 50_000  # 노트레이드 밴드: 이 금액 미만 '비중 조정' 주문 스킵
                                    # (순위 이탈 종목의 전량 매도에는 적용하지 않음)
REINVEST_CAP_RATIO        = 0.15    # 잔여현금 재투입 시 종목당 평가액 상한 (총예산 대비 15%)
SELL_SETTLE_WAIT_SECS     = 30      # 매도 체결 확인 polling 최대 대기 시간(초)
SELL_SETTLE_POLL_INTERVAL = 5       # polling 간격(초) — KIS 유량제한 고려
PRICE_CALL_SLEEP          = 0.2     # 시세조회 연속 호출 간격(초) — EGW00201 방지

# ── 현금 예치금 방화벽 ───────────────────────────────────────
# 봇이 절대 건드리지 않는 격리 현금액 (단위: 원), 기본값 0 = 전액 운용
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "0"))

# ── 강제 가상 테스트 모드 마스터 스위치 ─────────────────────
# True  → 주문 API 전송만 차단(Mock), 나머지 전 공정 100% 정상 실행
# False → 실전 모드, 실제 KIS API로 주문 전송
# ⚠️ 배포 전 반드시 False로 변경할 것!
# [임시] 2026-07-09 fix17 잔고 파싱 실계좌 검증용 True — 검증 후 즉시 False 복원
FORCE_TEST_MODE = True

# ── Lambda 스케줄 (EventBridge 설정 참고용) ──────────────────
# [fix15] 권장 시각 변경: 15:15는 동시호가(15:20~15:30) 직전이라 연속거래가
# 실질 5분뿐 → 미체결 위험. 시그널은 지난주 금요일 종가 기준이므로
# 실행을 앞당겨도 전략 왜곡 없음. EventBridge 콘솔에서 아래로 변경할 것:
# Lambda A (시그널 생성): 14:00 KST → Cron: 0 14 ? * MON *  (기존 15:05)
# Lambda B (주문 실행):   14:20 KST → Cron: 20 14 ? * MON *  (기존 15:15)

# ── KIS API ──────────────────────────────────────────────────
KIS_APPKEY    = os.environ.get("KIS_APPKEY",    "")
KIS_APPSECRET = os.environ.get("KIS_APPSECRET", "")
KIS_ACCOUNT   = os.environ.get("KIS_ACCOUNT",   "")
KIS_PRDT_CODE = "01"
URL_BASE      = "https://openapi.koreainvestment.com:9443"

# ── 텔레그램 알림 설정 ───────────────────────────────────────
# Lambda 콘솔 → 구성 → 환경 변수에 아래 두 키 등록
#   TELEGRAM_TOKEN   : BotFather에서 발급받은 봇 토큰
#   TELEGRAM_CHAT_ID : 메시지를 수신할 채팅방 ID
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
