# config.py — 글로벌 세팅 값 전용 (rambdaA)
# 버전: v1.0.20260705.1
import os

# ── S3 ──────────────────────────────────────────────────────
S3_BUCKET_NAME   = "eunsung-quant-guard-bucket"
SIGNAL_FILE_KEY  = "latest_signal.json"
QUANT_SIGNAL_KEY = "quant_signals.json"

# ── 운용 파라미터 ────────────────────────────────────────────
NUM_TARGETS  = 10   # 최종 매수 종목 수
BUDGET_RATIO = 1.0  # 국내주식 투자 비율 (1.0=100%, 0.5=50%)

VIX_TICKER      = "^VIX"
VIX_THRESHOLD   = 30.0   # BEAR 전환 임계값
BEAR_LIMIT_RATE = -0.01  # BEAR 지정가 매도 호가: 현재가 대비 -1%

# ── 현금 예치금 방화벽 ───────────────────────────────────────
# 봇이 절대 건드리지 않는 격리 현금액 (단위: 원), 기본값 0 = 전액 운용
CASH_RESERVE = int(os.environ.get("CASH_RESERVE", "0"))

# ── 강제 가상 테스트 모드 마스터 스위치 ─────────────────────
# True  → 주문 API 전송만 차단(Mock), 나머지 전 공정 100% 정상 실행
# False → 실전 모드, 실제 KIS API로 주문 전송
# ⚠️ 배포 전 반드시 False로 변경할 것!
FORCE_TEST_MODE = False

# ── Lambda 스케줄 (EventBridge 설정 참고용) ──────────────────
# Lambda A (시그널 생성): 15:05 KST → Cron: 5 15 ? * MON *
# Lambda B (주문 실행):   15:15 KST → Cron: 15 15 ? * MON *

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
