# lambda_function.py — Lambda B: 메인 제어 타워
# 버전: v1.0.20260709.1
# [변경 이력]
#   기능 1  : 주문 집행 완료 후 텔레그램 영수증 발송
#   기능 2  : 핵심 로직 전체 try-except + traceback 텔레그램 에러 자백
#   기능 3  : CASH_RESERVE 현금 방화벽
#   fix6    : FORCE_TEST_MODE 스위치 도입
#             — "총자산 0원 자동 가상전환 로직" 완전 삭제
#             — is_test 변수 제거, 오직 FORCE_TEST_MODE 하나로 제어
#
# AWS Lambda Handler: lambda_function.lambda_handler
# EventBridge: 타임존 Asia/Seoul / Cron: 15 15 ? * MON * (15:15 KST)

import json
import datetime
import traceback
import urllib3
import boto3
from config import (
    KIS_APPKEY, KIS_APPSECRET, KIS_ACCOUNT, KIS_PRDT_CODE,
    URL_BASE, S3_BUCKET_NAME, SIGNAL_FILE_KEY,
    CASH_RESERVE,
    FORCE_TEST_MODE,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from korea import run_korea_rebalancing
from usa   import run_usa_rebalancing


# ============================================================
# 텔레그램 발송 함수
# ============================================================

def send_telegram(message: str) -> None:
    """
    텔레그램 봇으로 메시지를 urllib3(내장) 기반으로 발송합니다.
    requests 패키지 의존성 제거 → Lambda 무설치 환경 안정화
    TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 미설정 시 조용히 스킵합니다.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 텔레그램 설정 미완료 → 알림 스킵")
        return
    try:
        http = urllib3.PoolManager()
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        body = json.dumps({
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        resp = http.request(
            "POST", url,
            headers={"Content-Type": "application/json"},
            body=body,
            timeout=10,
        )
        if resp.status == 200:
            print("✅ 텔레그램 발송 완료")
        else:
            print(f"⚠️ 텔레그램 발송 실패: {resp.status} / {resp.data[:200]}")
    except Exception as e:
        print(f"⚠️ 텔레그램 발송 중 예외 발생 (무시하고 계속 진행): {e}")


# ============================================================
# 주문 집행 완료 영수증 메시지 생성
# ============================================================

def build_execution_report(
    now_str: str,
    total_asset: int,
    investable_asset: int,
    cash_reserve: int,
    market_status: str,
    korea_result: dict,
    usa_result: dict,
    weekly_return_pct: float = None,
    prev_date: str = None,
) -> str:
    """매매 집행 완료 후 영수증 형태의 텔레그램 메시지를 생성합니다."""

    mode_tag    = "🧪 테스트 모드" if FORCE_TEST_MODE else "🚀 실전 모드"
    status_icon = "🚨 BEAR" if market_status == "BEAR" else "🟢 BULL"

    # [fix15] 종목명(코드) 병기
    name_map = korea_result.get("name_map") or {}

    def fmt(code) -> str:
        name = name_map.get(code)
        return f"{name}({code})" if name else str(code)

    lines = [
        f"🧾 <b>[QuantGuard] 자동매매 주문 집행 완료 보고서</b>  {mode_tag}",
        f"🕐 집행 시각: {now_str}",
        f"📊 시장 상태: <b>{status_icon}</b>",
        "─" * 32,
        "💰 <b>자산 현황</b>",
        f"   총 자산          : {total_asset:>15,} 원",
        f"   현금 예치금 차감  : {cash_reserve:>15,} 원",
        f"   실제 운용 자산    : {investable_asset:>15,} 원",
    ]

    # [fix15] 주간 수익률 (직전 실행 대비, 입출금 미반영)
    if weekly_return_pct is not None:
        lines.append(f"   전회({prev_date or '?'}) 대비: {weekly_return_pct:+.2f}% (입출금 미반영)")

    lines.append("─" * 32)
    lines.append("🇰🇷 <b>국내 ETF 매매 내역</b>")
    korea_res_code = korea_result.get("result", "")

    if korea_res_code == "BEAR_SHELTER_EXECUTED":
        lines.append("  ⛔ BEAR 대피: 전 종목 지정가(-1%) 매도 집행")
        failed_sells = korea_result.get("failed_sells", [])
        if failed_sells:
            lines.append(f"  🚨 <b>매도 실패 {len(failed_sells)}건 — 수동 확인 필요!</b>")
            for code, qty in failed_sells:
                lines.append(f"     - {fmt(code)}: {qty}주 미처분")
    elif korea_res_code in ("BEAR_SHELTER_ALREADY_CLEAN", "BEAR_SHELTER_CLEAN"):
        lines.append("  ✅ BEAR 대피: 이미 현금 상태 (매도 불필요)")
    elif korea_res_code in ("ZERO_ASSET_COMPLETED", "CASH_RESERVE_EXCEEDED"):
        lines.append("  ℹ️ 운용 가능 자산 0원 → 매도/매수 주문 0건으로 안전 완주")
    elif korea_res_code == "BULL_REBALANCING_SUCCESS":
        sell_orders = korea_result.get("sell_orders", [])
        buy_orders  = korea_result.get("buy_orders", [])

        if sell_orders:
            lines.append(f"  📤 <b>매도</b> ({len(sell_orders)}건)")
            for item in sell_orders:
                code = item[0] if isinstance(item, (list, tuple)) else item.get("code", "?")
                qty  = item[1] if isinstance(item, (list, tuple)) else item.get("qty", 0)
                lines.append(f"     - {fmt(code)}: {qty}주")
            if not korea_result.get("sell_settled", True):
                lines.append("  ⚠️ 일부 매도가 제한시간 내 체결 확인되지 않음")
        else:
            lines.append("  📤 매도 없음")

        if korea_result.get("buys_skipped_unsettled"):
            lines.append("  🚨 <b>매수 전체 스킵</b> — 매도 대금 미반영(예수금 0원). 수동 확인 필요")
        elif buy_orders:
            lines.append(f"  📥 <b>매수</b> ({len(buy_orders)}건)")
            for item in buy_orders:
                code  = item[0] if isinstance(item, (list, tuple)) else item.get("code", "?")
                qty   = item[1] if isinstance(item, (list, tuple)) else item.get("qty", 0)
                price = item[2] if isinstance(item, (list, tuple)) and len(item) > 2 else item.get("price", 0)
                lines.append(f"     - {fmt(code)}: {qty}주 @ {int(price):,}원")
        else:
            lines.append("  📥 매수 없음")

        # [fix15] 재투입 / 노트레이드 밴드 요약
        reinvest = korea_result.get("reinvest_orders", [])
        if reinvest:
            total_reinvest = sum(o["qty"] * o["limit_price"] for o in reinvest if o.get("ok"))
            lines.append(f"  ♻️ 잔여현금 재투입 {len(reinvest)}건 (≈{total_reinvest:,}원)")
        band = korea_result.get("skipped_band", [])
        if band:
            lines.append(f"  🙅 노트레이드 밴드 스킵 {len(band)}건 (비중 미세조정 생략)")

        # [fix15] 주문 실패 요약 (rt_cd 기반)
        failed = [o for o in korea_result.get("executed_orders", []) if not o.get("ok")]
        if failed:
            lines.append(f"  🚨 <b>주문 거부/실패 {len(failed)}건</b>")
            for o in failed:
                side = "매수" if o.get("side") == "BUY" else "매도"
                lines.append(f"     - [{side}] {fmt(o.get('code'))}: {o.get('qty')}주")
    else:
        lines.append(f"  ℹ️ 상태: {korea_res_code}")

    # [fix15] 시그널 검증 정보 (모멘텀 base 대조용)
    targets = korea_result.get("targets") or []
    if targets:
        lines.append("─" * 32)
        lines.append("🔎 <b>시그널 검증</b> (모멘텀 base → 현재)")
        for s in targets:
            base_d = s.get("base_date", "?")
            base_p = s.get("base_price")
            base_str = f"{base_d} {base_p:,.0f}원" if base_p else base_d
            lines.append(f"   {s.get('name', s.get('code'))}: "
                         f"{s.get('momentum', 0)*100:+.1f}% ({base_str})")

    lines.append("─" * 32)
    lines.append("🇺🇸 <b>미국 ETF 매매 내역</b>")
    usa_res = usa_result.get("result", "SKIPPED")
    if usa_res == "SKIPPED":
        lines.append(f"  ⏭ 스킵 ({usa_result.get('reason', 'BUDGET_RATIO=1.0')})")
    elif usa_res == "SKIPPED_BEAR":
        lines.append("  ⛔ BEAR 대피로 스킵")
    else:
        lines.append(f"  ℹ️ 상태: {usa_res}")

    lines.append("─" * 32)
    lines.append("✅ <b>모든 프로세스 안전 종료 완료</b>")
    lines.append("📌 상세 로그는 AWS CloudWatch에서 확인하세요.")

    return "\n".join(lines)


# ============================================================
# 호가 단위 및 지정가 계산
# ============================================================

def get_tick_size(price: float) -> int:
    """ETF 호가 단위: 가격대 무관 5원 고정"""
    return 5


def calc_limit_price(current_price: float, rate: float = -0.01) -> int:
    raw_price = current_price * (1 + rate)
    tick      = get_tick_size(raw_price)
    if rate >= 0:
        limit_price = (int(raw_price // tick) + 1) * tick
    else:
        limit_price = int(raw_price // tick) * tick
    return max(limit_price, tick)


# ============================================================
# 공통 증권사 통신 함수
# ============================================================

def get_access_token() -> str:
    http = urllib3.PoolManager()
    url  = f"{URL_BASE}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     KIS_APPKEY,
        "appsecret":  KIS_APPSECRET,
    }
    try:
        res      = http.request("POST", url,
                                headers={"content-type": "application/json"},
                                body=json.dumps(body).encode("utf-8"))
        res_data = json.loads(res.data.decode("utf-8"))
        token    = res_data.get("access_token")
        if not token:
            raise Exception(f"토큰 발급 실패: {res_data.get('error_description', '알 수 없음')}")
        return token
    except Exception as e:
        print(f"❌ 토큰 발급 에러: {e}")
        raise


def fetch_total_equity(token: str) -> int:
    """계좌 총평가금액 조회 (TR_ID: TTTC8434R)"""
    http = urllib3.PoolManager()
    url  = f"{URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "TTTC8434R",
    }
    # [fix17] 쿼리 파라미터 공식 규격(TTTC8434R) 정리 — korea.py와 동일 TR
    params = (
        f"?CANO={KIS_ACCOUNT}&ACNT_PRDT_CD={KIS_PRDT_CODE}"
        "&AFHR_FLPR_YN=N&OFL_YN=&INQR_DVSN=02&UNPR_DVSN=01"
        "&FUND_STTL_ICLD_YN=N&FNCG_AMT_AUTO_RDPT_YN=N&PRCS_DVSN=00"
        "&CTX_AREA_FK100=&CTX_AREA_NK100="
    )
    try:
        res      = http.request("GET", url + params, headers=headers)
        res_data = json.loads(res.data.decode("utf-8"))
        output2  = res_data.get("output2", [])
        if output2:
            return int(float(output2[0].get("tot_evlu_amt", 0)))
        raise Exception(f"잔고 데이터 없음: {res_data.get('msg1', '')}")
    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")
        raise


def execute_order(
    token: str,
    stock_code: str,
    quantity: int,
    is_buy: bool = True,
    limit_price: int = 0,
) -> str:
    """lambda_function.py 내부 usa.py 연동용 주문 함수 (FORCE_TEST_MODE 분기 포함)"""
    if quantity <= 0:
        return "9"

    label      = "매수" if is_buy else "매도"
    order_type = f"지정가({limit_price}원)" if limit_price > 0 else "시장가"

    if FORCE_TEST_MODE:
        print(f"🧪 [테스트 모드 주문 성공 시뮬레이션] "
              f"[{label} {order_type}] {stock_code} {quantity}주 — 실제 주문 미전송")
        return "0"

    # [fix14] korea.py fix13과 동일하게 TR_ID 교정 (TTTC0841U/0815U는 오류 원인)
    tr_id    = "TTTC0802U" if is_buy else "TTTC0801U"
    is_limit = limit_price > 0
    http     = urllib3.PoolManager()
    url      = f"{URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
    headers  = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }
    body = {
        "CANO":         KIS_ACCOUNT,
        "ACNT_PRDT_CD": KIS_PRDT_CODE,
        "PDNO":         stock_code,
        "ORD_DVSN":     "00" if is_limit else "01",
        "ORD_QTY":      str(quantity),
        "ORD_UNPR":     str(limit_price) if is_limit else "0",
    }
    try:
        res      = http.request("POST", url,
                                headers=headers,
                                body=json.dumps(body).encode("utf-8"))
        res_data = json.loads(res.data.decode("utf-8"))
        rt_cd    = res_data.get("rt_cd", "9")
        if rt_cd == "0":
            print(f"✅ [{label} {order_type} 성공] {stock_code} {quantity}주")
        else:
            print(f"❌ [{label} 거부] {stock_code}: {res_data.get('msg1', '')}")
        return rt_cd
    except Exception as e:
        print(f"❌ 주문 전송 에러 ({stock_code}): {e}")
        raise


# ============================================================
# Lambda 메인 핸들러
# ============================================================

def lambda_handler(event, context):
    if not all([KIS_APPKEY, KIS_APPSECRET, KIS_ACCOUNT]):
        msg = "❌ 환경변수 미설정"
        print(msg)
        return {"statusCode": 500, "body": msg}

    korea_time = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    now_str    = korea_time.strftime("%Y-%m-%d %H:%M:%S")

    mode_label = "🧪 테스트 모드 (주문 Mock)" if FORCE_TEST_MODE else "🚀 실전 모드 (실제 주문)"
    print(f"🕐 실행 시각 (KST): {now_str}")
    print(f"⚙️ FORCE_TEST_MODE: {FORCE_TEST_MODE} → {mode_label}")

    s3 = boto3.client("s3")

    # [fix15] 같은 날 중복 실행 가드
    # 2026-06-30 사고: 장중 수동 TEST 호출로 실전 주문 로직이 그대로 발사됨.
    # 오늘자 아카이브가 이미 있으면(=오늘 이미 실행됨) 재실행을 차단한다.
    # 의도적 재실행은 테스트 이벤트에 {"force_run": true}를 넣어 우회.
    if not (isinstance(event, dict) and event.get("force_run")):
        today_archive_key = f"latest_signal/{korea_time.strftime('%Y-%m-%d')}.json"
        try:
            s3.head_object(Bucket=S3_BUCKET_NAME, Key=today_archive_key)
            msg = (f"⛔ 오늘({korea_time.strftime('%Y-%m-%d')}) 이미 실행된 기록"
                   f"({today_archive_key})이 있어 중복 실행을 차단합니다. "
                   "의도적 재실행은 이벤트에 {\"force_run\": true}를 지정하세요.")
            print(msg)
            send_telegram("⛔ <b>[QuantGuard] 중복 실행 차단</b>\n" + msg)
            return {"statusCode": 200, "body": "DUPLICATE_RUN_BLOCKED"}
        except Exception:
            pass  # 오늘자 아카이브 없음(404 등) = 오늘 첫 실행 → 정상 진행

    try:
        token = get_access_token()

        # [fix14] 핸들러 자체 휴장일 체크 제거 — korea.py check_market_open()과 중복.
        # 짧은 시간 내 동일 API 연속 호출이 KIS 유량제한(EGW00201)을 유발해
        # 후속 잔고 조회가 비정상 응답(0원)을 받는 원인이 되었음.

        real_total_equity = fetch_total_equity(token)
        print(f"💰 계좌 총자산: {real_total_equity:,}원")

        print(f"🔒 현금 예치금(CASH_RESERVE): {CASH_RESERVE:,}원")
        investable_asset = real_total_equity - CASH_RESERVE

        if investable_asset < 0:
            print(
                f"⚠️ CASH_RESERVE({CASH_RESERVE:,}원)가 총자산({real_total_equity:,}원)을 초과! "
                "투자 가용 자산 0원 → 매매 없이 안전 종료합니다."
            )
            send_telegram(
                "⚠️ <b>[QuantGuard 경고]</b>\n"
                f"CASH_RESERVE({CASH_RESERVE:,}원)가 총자산({real_total_equity:,}원)을 초과했습니다.\n"
                "투자 가용 자산이 0원으로 강제 설정되어 이번 주 매매를 건너뜁니다.\n"
                "config.py의 CASH_RESERVE 값을 확인하세요."
            )
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "result":       "CASH_RESERVE_EXCEEDED",
                    "total_equity": real_total_equity,
                    "cash_reserve": CASH_RESERVE,
                    "investable":   0,
                }, ensure_ascii=False)
            }

        print(f"💡 실제 운용 가용액: {investable_asset:,}원")

        # [fix15] 주간 수익률: 직전 실행 시점의 총자산과 단순 비교 (입출금 미반영)
        prev_equity, prev_date = None, None
        try:
            _prev_obj  = s3.get_object(Bucket=S3_BUCKET_NAME, Key=SIGNAL_FILE_KEY)
            _prev_data = json.loads(_prev_obj["Body"].read().decode("utf-8"))
            prev_equity = _prev_data.get("total_equity_checked")
            prev_date   = str(_prev_data.get("updated_at", ""))[:10]
        except Exception:
            pass
        weekly_return_pct = None
        if prev_equity and prev_equity > 0:
            weekly_return_pct = round((real_total_equity / prev_equity - 1) * 100, 2)
            print(f"📈 전회 실행({prev_date}, {prev_equity:,}원) 대비 수익률: "
                  f"{weekly_return_pct:+.2f}%")

        # [fix14] 선행 조회한 총자산을 대체값으로 전달 — 내부 잔고 재조회가
        # 0원을 반환하는 이상 상황에서도 매매가 통째로 스킵되지 않도록 함
        korea_result = run_korea_rebalancing(
            token                 = token,
            fallback_total_equity = real_total_equity,
        )

        # [fix15] VIX 판별 불가 → 안전 스킵 (조용히 끝내지 않고 텔레그램 경고)
        if korea_result.get("result") == "VIX_UNKNOWN_SKIP":
            send_telegram(
                "⚠️ <b>[QuantGuard] VIX 판별 불가 — 리밸런싱 안전 스킵</b>\n"
                "Lambda A가 VIX 수집과 직전값 carry-over에 모두 실패했습니다.\n"
                "이번 주 매매를 건너뛰고 기존 포지션을 유지합니다.\n"
                "야후 파이낸스 상태와 CloudWatch 로그를 확인하세요."
            )
            return {"statusCode": 200,
                    "body": json.dumps(korea_result, ensure_ascii=False)}

        if korea_result.get("result") in (
            "S3_SIGNAL_ERROR", "NO_TARGETS", "STALE_SIGNAL_ABORT", "MARKET_CLOSED"
        ):
            return {"statusCode": 200,
                    "body": json.dumps(korea_result, ensure_ascii=False)}

        # [fix15] "BEAR_SHELTER_ALREADY_CLEAN"이 목록에 없어 BULL 경로로 새던 버그 수정
        if korea_result.get("result") in (
            "BEAR_SHELTER_EXECUTED", "BEAR_SHELTER_CLEAN", "BEAR_SHELTER_ALREADY_CLEAN"
        ):
            print("🚨 BEAR 대피 완료 → 미국 ETF 스킵")
            output_signal = {
                "updated_at":           now_str,
                "market_status":        "BEAR",
                "force_test_mode":      FORCE_TEST_MODE,
                "total_equity_checked": real_total_equity,
                "cash_reserve":         CASH_RESERVE,
                "investable_asset":     investable_asset,
                "prev_equity":          prev_equity,
                "prev_date":            prev_date,
                "weekly_return_pct":    weekly_return_pct,
                "korea":                korea_result,
                "usa":                  {"result": "SKIPPED_BEAR"},
            }
            body_bear = json.dumps(output_signal, ensure_ascii=False, indent=2)
            # ① 최신본 (덮어쓰기)
            s3.put_object(Bucket=S3_BUCKET_NAME, Key=SIGNAL_FILE_KEY, Body=body_bear)
            # ② 날짜별 아카이브
            archive_key = f"latest_signal/{korea_time.strftime('%Y-%m-%d')}.json"
            s3.put_object(Bucket=S3_BUCKET_NAME, Key=archive_key, Body=body_bear)
            print(f"✅ S3 아카이브 완료: {archive_key}")
            report = build_execution_report(
                now_str, real_total_equity, investable_asset, CASH_RESERVE,
                "BEAR", korea_result, {"result": "SKIPPED_BEAR"},
                weekly_return_pct=weekly_return_pct, prev_date=prev_date,
            )
            send_telegram(report)
            return {"statusCode": 200,
                    "body": json.dumps(output_signal, ensure_ascii=False)}

        usa_result = run_usa_rebalancing(
            token             = token,
            real_total_equity = investable_asset,
            is_test           = FORCE_TEST_MODE,
            execute_order_fn  = execute_order,
        )

        output_signal = {
            "updated_at":           now_str,
            "market_status":        korea_result.get("market_status", "BULL"),
            "force_test_mode":      FORCE_TEST_MODE,
            "total_equity_checked": real_total_equity,
            "cash_reserve":         CASH_RESERVE,
            "investable_asset":     investable_asset,
            # [fix15] 주간 성과 추적 필드
            "prev_equity":          prev_equity,
            "prev_date":            prev_date,
            "weekly_return_pct":    weekly_return_pct,
            "korea": {
                "sell_orders":            korea_result.get("sell_orders", []),
                "buy_orders":             korea_result.get("buy_orders", []),
                "executed_orders":        korea_result.get("executed_orders", []),
                "reinvest_orders":        korea_result.get("reinvest_orders", []),
                "skipped_band":           korea_result.get("skipped_band", []),
                "buys_skipped_unsettled": korea_result.get("buys_skipped_unsettled", False),
                "sell_settled":           korea_result.get("sell_settled", True),
            },
            "usa": usa_result,
        }
        body_bull = json.dumps(output_signal, ensure_ascii=False, indent=2)
        # ① 최신본 (덮어쓰기)
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=SIGNAL_FILE_KEY, Body=body_bull)
        # ② 날짜별 아카이브
        archive_key = f"latest_signal/{korea_time.strftime('%Y-%m-%d')}.json"
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=archive_key, Body=body_bull)
        print(f"✅ S3 아카이브 완료: {archive_key}")

        print("✅ 전체 리밸런싱 완료")

        report = build_execution_report(
            now_str,
            real_total_equity,
            investable_asset,
            CASH_RESERVE,
            korea_result.get("market_status", "BULL"),
            korea_result,
            usa_result,
            weekly_return_pct=weekly_return_pct,
            prev_date=prev_date,
        )
        print("\n📱 텔레그램 영수증 발송 중...")
        send_telegram(report)

        return {"statusCode": 200,
                "body": json.dumps(output_signal, ensure_ascii=False)}

    except Exception as e:
        tb_str = traceback.format_exc()
        error_msg = (
            "🚨 <b>[QuantGuard 시스템 에러 발생]</b>\n"
            "❌ <b>위치</b>: rambdaB / lambda_function.py\n"
            f"📟 <b>메시지</b>: {str(e)}\n"
            f"📝 <b>상세 정보 (Traceback)</b>:\n<pre>{tb_str[:3000]}</pre>\n"
            "\n⚠️ <i>AWS CloudWatch 로그를 확인하기 전에 위 내용을 먼저 점검하세요.</i>"
        )
        print(f"🚨 치명적 에러 발생:\n{tb_str}")
        try:
            send_telegram(error_msg)
        except Exception:
            pass
        raise e