# korea.py — Lambda B: 국내 주식 주문 집행 모듈
# 버전: v1.0.20260708.1
# 수정 이력:
#   제미나이     : 실시간 현재가, 총자산 직접계산, BEAR 조기종료, 예수금 검증
#   Claude fix1  : TR_ID 오류 수정 (TTTC0802U→TTTC0841U 등)
#   Claude fix2  : config 변수명 통일
#   제미나이 fix3: buy_orders 튜플 2개→3개 (ValueError 수정)
#   제미나이 fix4: fetch_available_cash TR_ID TTTC8434R→TTTC8408R 수정
#   Claude fix5  : CASH_RESERVE 방화벽, 영수증용 반환값 추가
#   Claude fix6  : FORCE_TEST_MODE 스위치 — execute_order Mock 처리

import time
import json
import boto3
import urllib3
from datetime import datetime
from config import (
    URL_BASE,
    KIS_APPKEY,
    KIS_APPSECRET,
    KIS_ACCOUNT,
    KIS_PRDT_CODE,
    S3_BUCKET_NAME,
    QUANT_SIGNAL_KEY,
    NUM_TARGETS,
    EXIT_RANK_BUFFER,
    BUDGET_RATIO,
    BEAR_LIMIT_RATE,
    CASH_RESERVE,
    FORCE_TEST_MODE,
    MIN_ORDER_VALUE,
    REINVEST_CAP_RATIO,
    SELL_SETTLE_WAIT_SECS,
    SELL_SETTLE_POLL_INTERVAL,
    PRICE_CALL_SLEEP,
)

BULL_LIMIT_RATE     = 0.01
SIGNAL_MAX_AGE_SECS = 3600


# ============================================================
# 호가 단위 및 지정가 계산
# ============================================================

def get_tick_size(price: float) -> int:
    """ETF 호가 단위 반환 (가격대 무관 5원 고정)"""
    return 5


def calc_limit_price(current_price: float, rate: float) -> int:
    raw_price = current_price * (1 + rate)
    tick      = get_tick_size(raw_price)
    if rate >= 0:
        limit_price = (int(raw_price // tick) + 1) * tick
    else:
        limit_price = int(raw_price // tick) * tick
    return max(limit_price, tick)


# ============================================================
# KIS API 조회 함수
# ============================================================

def get_realtime_price(token: str, code: str) -> int:
    """KIS API 실시간 현재가 조회 (TR_ID: FHKST01010100)"""
    http = urllib3.PoolManager()
    url  = (f"{URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
            f"?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD={code}")
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "FHKST01010100",
        "custtype":      "P",
    }
    try:
        res      = http.request("GET", url, headers=headers)
        res_data = json.loads(res.data.decode("utf-8"))
        if res_data.get("rt_cd") == "0":
            return int(res_data["output"].get("stck_prpr", 0))
    except Exception as e:
        print(f"⚠️ {code} 현재가 조회 실패: {e}")
    return 0


def check_market_open(token: str) -> bool:
    """KIS API 휴장일 조회로 오늘 개장 여부 확인"""
    http      = urllib3.PoolManager()
    today_str = datetime.today().strftime("%Y%m%d")
    url       = (f"{URL_BASE}/uapi/domestic-stock/v1/quotations/chk-holiday"
                 f"?BASS_DT={today_str}&CTX_AREA_NK=&CTX_AREA_FK=")
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "CTCA0903R",
        "custtype":      "P",
    }
    try:
        res      = http.request("GET", url, headers=headers)
        res_data = json.loads(res.data.decode("utf-8"))
        for item in res_data.get("output", []):
            if item.get("bass_dt") == today_str:
                return item.get("opnd_yn", "N") == "Y"
    except Exception as e:
        print(f"⚠️ 휴장일 조회 실패 ({e}) → 개장일로 간주하고 진행")
    return True


def fetch_present_holdings(token: str, max_retries: int = 3) -> tuple:
    """보유 종목 + 계좌 총평가금액 동시 반환 (TTTC8434R 1회 호출로 두 값 확보)

    [fix11] 반환값 변경: dict → (dict, int)
    - [0] holdings     : 보유 종목 dict
    - [1] tot_evlu_amt : output2의 계좌 총평가금액 (예수금+주식 합산, 증권사 공식값)
    - 100% 현금 상태에서 fetch_available_cash()가 0을 반환하는 버그를 우회
    [fix14] rt_cd 검증 + 재시도 도입
    - KIS 유량제한(EGW00201) 등 비정상 응답 시 조용히 0원을 반환하던 버그 수정
    - 비정상 응답이면 1초 간격 재시도, 전부 실패하면 예외 발생
      (총자산 0원 오인 → 매매 전체 스킵 사고 방지)
    """
    http = urllib3.PoolManager()
    url  = (f"{URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
            f"?CANO={KIS_ACCOUNT}&ACNT_PRDT_CD={KIS_PRDT_CODE}"
            "&AFHR_FLG=00&OVR_FLG=00&PRCS_DVSN=00&INQR_DVSN=00"
            "&CTX_AREA_FK100=&CTX_AREA_NK100=")
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "TTTC8434R",
    }
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            res      = http.request("GET", url, headers=headers)
            res_data = json.loads(res.data.decode("utf-8"))
        except Exception as e:
            last_error = f"통신 오류: {e}"
            print(f"⚠️ 잔고 조회 통신 실패 ({attempt}/{max_retries}): {e}")
            time.sleep(1)
            continue

        rt_cd   = res_data.get("rt_cd")
        output2 = res_data.get("output2", [])

        if rt_cd == "0" and output2:
            holdings = {}
            for item in res_data.get("output1", []):
                qty  = int(item.get("hldn_qty", 0))
                code = item.get("pdno", "")
                if code and qty > 0:
                    holdings[code] = {
                        "qty":  qty,
                        "prpr": int(float(item.get("prpr", 0))),
                        "name": item.get("prdt_name", code),
                    }
            tot_evlu_amt = int(float(output2[0].get("tot_evlu_amt", 0)))
            return holdings, tot_evlu_amt

        last_error = (f"rt_cd={rt_cd}, msg_cd={res_data.get('msg_cd', '')}, "
                      f"msg1={res_data.get('msg1', '')}")
        print(f"⚠️ 잔고 조회 비정상 응답 ({attempt}/{max_retries}): {last_error}")
        time.sleep(1)

    # 조용한 0원 반환 대신 예외 → 핸들러가 텔레그램으로 에러 자백
    raise Exception(f"잔고 조회 {max_retries}회 모두 실패: {last_error}")


def fetch_available_cash(token: str) -> int:
    """주문 가능 예수금 조회 (TR_ID: TTTC8408R)"""
    http = urllib3.PoolManager()
    url  = (f"{URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
            f"?CANO={KIS_ACCOUNT}&ACNT_PRDT_CD={KIS_PRDT_CODE}"
            "&PDNO=005930&ORD_UNPR=0&ORD_DVSN=01"
            "&CMA_EVLU_AMT_ICLD_YN=N&OVRS_ICLD_YN=N")
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "TTTC8408R",
    }
    try:
        res      = http.request("GET", url, headers=headers)
        res_data = json.loads(res.data.decode("utf-8"))
        if res_data.get("rt_cd") == "0":
            return int(float(res_data["output"].get("ord_psbl_cash_amt", 0)))
    except Exception as e:
        print(f"⚠️ 예수금 조회 실패: {e}")
    return 0


# ============================================================
# 주문 실행 함수
# ============================================================

def execute_order(token: str, code: str, qty: int,
                  is_buy: bool, limit_price: int = 0) -> bool:
    if qty <= 0:
        return False

    label      = "매수" if is_buy else "매도"
    order_type = f"지정가({limit_price:,}원)" if limit_price > 0 else "시장가"

    if FORCE_TEST_MODE:
        print(f"🧪 [테스트 모드 주문 성공 시뮬레이션] "
              f"[{label} {order_type}] {code} {qty}주 — 실제 주문 미전송")
        return True

    # [fix13] 국내 주식 현물 현금 주문 TR_ID 교정
    # 기존 TTTC0841U(매수)/TTTC0815U(매도) → 오류 원인
    # 정정 TTTC0802U(매수)/TTTC0801U(매도) — KIS 공식 명세 기준
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
        "custtype":      "P",   # [fix13] 개인 고객 식별 헤더 추가 (필수)
    }
    body = {
        "CANO":         KIS_ACCOUNT,
        "ACNT_PRDT_CD": KIS_PRDT_CODE,
        "PDNO":         code,
        "ORD_DVSN":     "00" if is_limit else "01",
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     str(limit_price) if is_limit else "0",
    }
    try:
        res      = http.request("POST", url, headers=headers,
                                body=json.dumps(body).encode("utf-8"))
        res_data = json.loads(res.data.decode("utf-8"))
        if res_data.get("rt_cd") == "0":
            print(f"✅ [{label} {order_type} 성공] {code} {qty}주")
            return True
        else:
            print(f"❌ [{label} 실패] {code}: {res_data.get('msg1', '')}")
    except Exception as e:
        print(f"❌ 주문 전송 에러 ({code}): {e}")
    return False


# ============================================================
# [fix15] 매도 체결 확인 polling — 고정 10초 대기 대체
# ============================================================

def wait_sell_settlement(token: str, full_sell_codes: set) -> bool:
    """전량 매도 종목의 보유수량이 0이 될 때까지 잔고를 polling.

    - 반환 True  : 전량 매도 체결 확인 (또는 부분 매도만 있어 단기 대기로 충분)
    - 반환 False : 제한시간 내 미체결 잔존 → 호출부가 매수 예산을 보수적으로 처리
    - polling 간격은 KIS 유량제한(EGW00201)을 고려해 SELL_SETTLE_POLL_INTERVAL초
    """
    if not full_sell_codes:
        # 부분 매도만 있는 경우: 수량 추적 대신 짧은 고정 대기
        print(f"⏳ 부분 매도 체결 대기 ({SELL_SETTLE_POLL_INTERVAL}초)...")
        time.sleep(SELL_SETTLE_POLL_INTERVAL)
        return True

    deadline = time.time() + SELL_SETTLE_WAIT_SECS
    while time.time() < deadline:
        time.sleep(SELL_SETTLE_POLL_INTERVAL)
        try:
            holdings, _ = fetch_present_holdings(token)
        except Exception as e:
            print(f"⚠️ 체결확인 잔고조회 실패 ({e}) → 다음 주기 재시도")
            continue
        pending = [c for c in full_sell_codes if holdings.get(c, {}).get("qty", 0) > 0]
        if not pending:
            print("✅ 매도 전량 체결 확인")
            return True
        print(f"⏳ 매도 체결 대기 중... 미체결 {len(pending)}종목: {pending}")

    print(f"⚠️ {SELL_SETTLE_WAIT_SECS}초 내 전량 매도 미체결 → 매수 예산 보수 처리")
    return False


# ============================================================
# [fix15] 잔여현금 재투입 — 모멘텀 순위 순환식 + 종목당 상한
# ============================================================

def reinvest_leftover_cash(token: str, leftover_cash: float,
                           candidates: list, kr_budget: float,
                           name_map: dict) -> list:
    """1차 매수 후 남은 현금을 모멘텀 순위 순으로 1주씩 순환 배분.

    - candidates: [{"code", "qty_total", "limit_price"}] 모멘텀 순 정렬
    - 종목당 평가액이 kr_budget * REINVEST_CAP_RATIO 를 넘지 않도록 상한 적용
    - 수량을 먼저 시뮬레이션으로 확정한 뒤 종목당 1회 주문으로 집행 (호출 최소화)
    """
    cap    = kr_budget * REINVEST_CAP_RATIO
    extra  = {c["code"]: 0 for c in candidates}
    value  = {c["code"]: c["qty_total"] * c["limit_price"] for c in candidates}

    progress = True
    while progress:
        progress = False
        for c in candidates:
            code, price = c["code"], c["limit_price"]
            if price <= 0:
                continue
            if leftover_cash >= price and value[code] + price <= cap:
                extra[code]   += 1
                value[code]   += price
                leftover_cash -= price
                progress       = True

    orders = []
    for c in candidates:
        code, price = c["code"], c["limit_price"]
        qty = extra[code]
        if qty <= 0:
            continue
        ok = execute_order(token, code, qty, is_buy=True, limit_price=price)
        orders.append({"code": code, "qty": qty, "limit_price": price, "ok": ok})
        print(f"♻️ [재투입 매수] {name_map.get(code, code)}({code}) {qty}주 @ {price:,}원"
              + ("" if ok else " ❌ 실패"))
        time.sleep(PRICE_CALL_SLEEP)

    if not orders:
        print(f"💤 재투입 대상 없음 (잔여 {leftover_cash:,.0f}원, 종목당 상한 {cap:,.0f}원)")
    return orders


# ============================================================
# 메인 리밸런싱 함수
# ============================================================

def run_korea_rebalancing(token: str, fallback_total_equity: int = 0) -> dict:
    """국내 리밸런싱 실행

    [fix14] fallback_total_equity: 핸들러가 선행 조회한 계좌 총평가금액.
    이 함수 내부의 잔고 재조회가 0원을 반환하는 이상 상황에서 대체값으로 사용.
    """
    mode_label = "🧪 테스트 모드 (주문 Mock)" if FORCE_TEST_MODE else "🚀 실전 모드 (실제 주문)"
    print(f"⚙️ 실행 모드: {mode_label}")

    print("⏳ 오늘 개장 여부 확인 중...")
    if not check_market_open(token):
        print("😴 오늘은 휴장일 → 매매 없이 종료")
        return {"result": "MARKET_CLOSED", "sell_orders": [], "buy_orders": []}

    print("⏳ S3 시그널 파일 조회 중...")
    s3 = boto3.client("s3")
    try:
        s3_obj      = s3.get_object(Bucket=S3_BUCKET_NAME, Key=QUANT_SIGNAL_KEY)
        signal_data = json.loads(s3_obj["Body"].read().decode("utf-8"))
    except Exception as e:
        print(f"❌ S3 시그널 조회 실패 ({e}) → 안전 종료")
        return {"result": "S3_SIGNAL_ERROR", "sell_orders": [], "buy_orders": []}

    try:
        updated_at  = datetime.strptime(signal_data["updated_at"], "%Y-%m-%d %H:%M:%S")
        age_seconds = (datetime.now() - updated_at).total_seconds()
        if age_seconds > SIGNAL_MAX_AGE_SECS:
            print(f"🚨 시그널 만료 ({age_seconds/60:.0f}분) → 매매 중단")
            return {"result": "STALE_SIGNAL_ABORT", "sell_orders": [], "buy_orders": []}
    except Exception as e:
        print(f"⚠️ 신선도 체크 실패 ({e}) → 계속 진행")

    market_status = signal_data.get("market_status", "BULL")
    target_stocks = signal_data.get("top_10_stocks", [])

    # [fix16] 순위 히스테리시스: candidates(1~15위) → code:rank 매핑
    # 구버전 시그널(candidates 없음) 폴백: top_10_stocks만으로 1~10위 매핑
    # (11~15위 정보 없음 → 사실상 비활성, 기존 동작과 동일)
    _candidates = signal_data.get("candidates")
    if _candidates:
        rank_map = {c["code"]: c.get("rank") for c in _candidates}
    else:
        rank_map = {s["code"]: i for i, s in enumerate(target_stocks, 1)}

    # [fix15] VIX 판별 불가(UNKNOWN) → 매매 없이 안전 스킵
    # Lambda A가 VIX 수집·carry-over 모두 실패했을 때만 오는 상태.
    # BULL로 가정하고 매수하는 것보다 한 주 쉬는 쪽이 안전.
    if market_status == "UNKNOWN":
        print("⚠️ VIX 판별 불가(UNKNOWN) → 이번 주 리밸런싱 스킵 (안전 유지)")
        return {"result": "VIX_UNKNOWN_SKIP", "sell_orders": [], "buy_orders": []}

    if market_status == "BEAR":
        print("🚨 BEAR 시그널 → 전량 지정가(-1%) 매도 후 현금 대피")
        current_holdings, _ = fetch_present_holdings(token)

        if not current_holdings:
            print("✅ 보유 종목 없음 → 이미 현금 상태")
            return {
                "result":        "BEAR_SHELTER_ALREADY_CLEAN",
                "market_status": "BEAR",
                "sell_orders":   [],
                "buy_orders":    [],
            }

        # [fix15] rt_cd 검증 + 실패 시 시장가 재시도
        # 기존엔 execute_order 반환값을 무시하고 무조건 '대피 완료'로 보고했음.
        # 폭락장 갭하락으로 -1% 지정가가 거부/미체결 소지가 있는 상황에서
        # 대피 실패를 성공으로 오보고하는 최악의 시나리오 차단.
        bear_sells      = []
        executed_orders = []
        failed_sells    = []
        name_map        = {c: i["name"] for c, i in current_holdings.items()}

        for code, info in current_holdings.items():
            qty      = info["qty"]
            realtime = get_realtime_price(token, code)
            time.sleep(PRICE_CALL_SLEEP)
            price    = realtime if realtime > 0 else info["prpr"]
            limit_p  = calc_limit_price(price, BEAR_LIMIT_RATE)

            ok      = execute_order(token, code, qty, is_buy=False, limit_price=limit_p)
            retried = False
            if not ok:
                print(f"⚠️ [BEAR 매도 실패] {info['name']}({code}) 지정가 거부 → 시장가 재시도")
                time.sleep(1)
                ok      = execute_order(token, code, qty, is_buy=False, limit_price=0)
                retried = True

            executed_orders.append({
                "side": "SELL", "code": code, "qty": qty,
                "limit_price": limit_p if not retried else 0,
                "ok": ok, "retried": retried,
            })
            if ok:
                bear_sells.append((code, qty))
                print(f"♻️ [BEAR 매도] {info['name']}({code}) "
                      f"현재가:{price:,} → 지정가:{limit_p:,} {qty}주"
                      + (" (시장가 재시도)" if retried else ""))
            else:
                failed_sells.append((code, qty))
                print(f"❌ [BEAR 매도 최종 실패] {info['name']}({code}) {qty}주 — 수동 확인 필요")

        return {
            "result":          "BEAR_SHELTER_EXECUTED",
            "market_status":   "BEAR",
            "sell_orders":     bear_sells,
            "buy_orders":      [],
            "executed_orders": executed_orders,
            "failed_sells":    failed_sells,
            "name_map":        name_map,
        }

    if not target_stocks:
        print("⚠️ 매수 종목 0개 → 종료")
        return {"result": "NO_TARGETS", "sell_orders": [], "buy_orders": []}

    print(f"📈 BULL 시그널 → 리밸런싱 시작 (목표 {NUM_TARGETS}개)")

    current_holdings, raw_total_asset = fetch_present_holdings(token)

    # [fix14] 재조회가 0원인데 핸들러 선행 조회값이 있으면 그 값으로 대체
    # (2026-06-30 사고: 두 번째 잔고 조회만 0원 → 매수 전체 스킵)
    if raw_total_asset <= 0 < fallback_total_equity:
        print(f"⚠️ 잔고 재조회 총평가금액 0원 → 핸들러 선행 조회값 "
              f"{fallback_total_equity:,}원으로 대체")
        raw_total_asset = fallback_total_equity

    # [fix11] tot_evlu_amt(증권사 공식 총평가금액)로 총자산 확정
    # 기존: fetch_available_cash() + stock_value 합산 방식
    # → 100% 현금 상태에서 fetch_available_cash()가 0 반환 시 총자산 0원 오산출 버그 수정
    print(
        f"💰 총자산(증권사 공식): {raw_total_asset:,}원\n"
        f"🔒 CASH_RESERVE 차감: -{CASH_RESERVE:,}원"
    )
    total_asset = max(0, raw_total_asset - CASH_RESERVE)

    if total_asset <= 0:
        print("⚠️ 운용 가능 자산 0원 → 매도/매수 주문 0건으로 프로세스 완주")
        return {
            "result":        "ZERO_ASSET_COMPLETED",
            "market_status": "BULL",
            "sell_orders":   [],
            "buy_orders":    [],
            "sell_count":    0,
            "buy_count":     0,
        }

    kr_budget    = total_asset * BUDGET_RATIO
    budget_per   = kr_budget / NUM_TARGETS if NUM_TARGETS > 0 else 0
    targets      = target_stocks[:NUM_TARGETS]
    target_codes = [s["code"] for s in targets]
    print(f"🎯 국내 배정 예산: {kr_budget:,.0f}원 / 종목당: {budget_per:,.0f}원")

    # [fix15] 영수증 종목명 표기용 매핑 (시그널 name + 보유종목 prdt_name)
    name_map = {s["code"]: s.get("name", s["code"]) for s in targets}
    for _c, _i in current_holdings.items():
        name_map.setdefault(_c, _i.get("name", _c))

    sell_orders     = []     # (code, qty)
    buy_orders      = []     # (code, qty, price)
    skipped_band    = []     # [fix15] 노트레이드 밴드로 스킵한 비중조정 주문
    full_sell_codes = set()  # 순위 이탈 전량 매도 종목 (체결 polling 대상)

    for code, info in current_holdings.items():
        realtime = get_realtime_price(token, code)
        time.sleep(PRICE_CALL_SLEEP)   # [fix15] 유량제한 방지
        price    = realtime if realtime > 0 else info["prpr"]

        if code in target_codes:
            # 상위 10위 이내 보유 종목: 기존 로직 그대로 (밴드 적용 비중조정)
            target_qty = int(budget_per // price) if price > 0 else 0
            diff       = target_qty - info["qty"]
            if diff < 0:
                # [fix15] 노트레이드 밴드: 비중 미세조정 매도는 스킵 (슬리피지 절감)
                trade_value = abs(diff) * price
                if trade_value < MIN_ORDER_VALUE:
                    skipped_band.append({"code": code, "side": "SELL",
                                         "qty": abs(diff), "value": trade_value})
                    print(f"🙅 [밴드 스킵] {name_map.get(code, code)}({code}) "
                          f"매도 {abs(diff)}주 ≈{trade_value:,.0f}원 < {MIN_ORDER_VALUE:,}원")
                else:
                    sell_orders.append((code, abs(diff)))
        elif rank_map.get(code) is not None and rank_map[code] <= EXIT_RANK_BUFFER:
            # [fix16 히스테리시스] 11~15위: 매도하지 않고 보유 유지, 비중조정도 하지 않음
            # 진입은 top10, 퇴출은 15위 밖 — 경계 종목의 주간 왕복매매 제거
            print(f"🛡 [히스테리시스 유지] {name_map.get(code, code)}({code}) "
                  f"현재 순위 {rank_map[code]}위 (퇴출 기준 {EXIT_RANK_BUFFER}위 밖)")
        else:
            # 15위 밖(또는 랭크 정보 없음, 구버전 시그널 포함): 전량 매도 — 밴드 적용 안 함
            sell_orders.append((code, info["qty"]))
            full_sell_codes.add(code)

    for stock in targets:
        code     = stock["code"]
        realtime = get_realtime_price(token, code)
        time.sleep(PRICE_CALL_SLEEP)   # [fix15] 유량제한 방지
        price    = realtime if realtime > 0 else int(stock["price"])

        if price == 0:
            print(f"⚠️ {code} 현재가 조회 완전 실패 → 매수 스킵")
            continue

        target_qty   = int(budget_per // price)
        already_have = current_holdings.get(code, {}).get("qty", 0)
        diff         = target_qty - already_have

        if diff > 0:
            # [fix15] 노트레이드 밴드: 이미 보유 중인 종목의 미세 추가매수는 스킵
            # (신규 진입 종목은 diff가 종목당 예산 규모라 밴드에 걸리지 않음)
            trade_value = diff * price
            if already_have > 0 and trade_value < MIN_ORDER_VALUE:
                skipped_band.append({"code": code, "side": "BUY",
                                     "qty": diff, "value": trade_value})
                print(f"🙅 [밴드 스킵] {name_map.get(code, code)}({code}) "
                      f"매수 {diff}주 ≈{trade_value:,.0f}원 < {MIN_ORDER_VALUE:,}원")
            else:
                buy_orders.append((code, diff, price))

    print(f"📋 매도 {len(sell_orders)}건 / 매수 {len(buy_orders)}건 / "
          f"밴드 스킵 {len(skipped_band)}건")

    executed_orders = []

    if sell_orders:
        print(f"⏳ 매도 {len(sell_orders)}건 집행 중...")
        for code, qty in sell_orders:
            realtime = get_realtime_price(token, code)
            time.sleep(PRICE_CALL_SLEEP)
            price    = realtime if realtime > 0 else current_holdings.get(code, {}).get("prpr", 0)
            limit_p  = calc_limit_price(price, BEAR_LIMIT_RATE)
            ok       = execute_order(token, code, qty, is_buy=False, limit_price=limit_p)
            executed_orders.append({"side": "SELL", "code": code, "qty": qty,
                                    "limit_price": limit_p, "ok": ok})
            print(f"♻️ [매도] {name_map.get(code, code)}({code}) "
                  f"현재가:{price:,} → 지정가:{limit_p:,} {qty}주")

        # [fix15] 고정 10초 대기 → 체결확인 polling
        settled = wait_sell_settlement(token, full_sell_codes)
    else:
        settled = True
        print("✅ 매도 종목 없음")

    reinvest_orders        = []
    buys_skipped_unsettled = False

    if buy_orders:
        available_cash = fetch_available_cash(token)

        if available_cash <= 0:
            if not sell_orders:
                # [fix12 유지] 매도가 없었는데 0원 → 100% 현금 상태에서
                # TTTC8408R이 0을 반환하는 quirk로 간주, 운용 예산으로 대체
                available_cash = total_asset
                print(f"⚠️ 예수금 조회 0원 (매도 없음) → 운용 예산으로 대체: {available_cash:,}원")
            else:
                # [fix15] 매도가 있었는데 예수금 0원 → 미체결 가능성.
                # 총평가액 대체(구 fix12)는 없는 돈으로 매수를 시도하는 셈이라 폐지.
                buys_skipped_unsettled = True
                reason = "매도 미체결" if not settled else "체결 확인됐으나 예수금 미반영"
                print(f"⚠️ {reason} + 예수금 0원 → 매수 전체 스킵 (예산 과다계산 방지)")
        else:
            print(f"💵 매수 전 가용 예수금: {available_cash:,}원")

        if not buys_skipped_unsettled:
            print(f"⏳ 매수 {len(buy_orders)}건 집행 중...")
            reinvest_candidates = []   # [fix15] 재투입 후보 (모멘텀 순 유지)

            for code, qty, c_price in buy_orders:
                final_price = get_realtime_price(token, code)
                time.sleep(PRICE_CALL_SLEEP)
                use_price   = final_price if final_price > 0 else c_price
                limit_p     = calc_limit_price(use_price, BULL_LIMIT_RATE)
                required    = limit_p * qty

                if required > available_cash:
                    adjusted = int(available_cash // limit_p)
                    if adjusted <= 0:
                        print(f"⚠️ {code} 예수금 부족 ({available_cash:,}원) → 스킵")
                        continue
                    print(f"⚠️ {code} 예수금 부족 → {qty}주 → {adjusted}주로 축소")
                    qty      = adjusted
                    required = limit_p * qty

                success = execute_order(token, code, qty, is_buy=True, limit_price=limit_p)
                executed_orders.append({"side": "BUY", "code": code, "qty": qty,
                                        "limit_price": limit_p, "ok": success})
                if success:
                    available_cash -= required
                    already = current_holdings.get(code, {}).get("qty", 0)
                    reinvest_candidates.append({"code": code,
                                                "qty_total": already + qty,
                                                "limit_price": limit_p})
                print(f"🔥 [매수] {name_map.get(code, code)}({code}) "
                      f"현재가:{use_price:,} → 지정가:{limit_p:,} {qty}주")

            # [fix15] 잔여현금 재투입 (모멘텀 순위 순환식, 종목당 상한 15%)
            if available_cash > 0 and reinvest_candidates:
                print(f"💰 잔여현금 {available_cash:,.0f}원 재투입 시도...")
                reinvest_orders = reinvest_leftover_cash(
                    token, available_cash, reinvest_candidates, kr_budget, name_map)
                executed_orders += [{"side": "BUY", "code": o["code"], "qty": o["qty"],
                                     "limit_price": o["limit_price"], "ok": o["ok"],
                                     "reinvest": True} for o in reinvest_orders]
    else:
        print("✅ 매수 종목 없음")

    print("🏁 리밸런싱 완료")
    return {
        "result":                 "BULL_REBALANCING_SUCCESS",
        "market_status":          "BULL",
        "sell_orders":            sell_orders,
        "buy_orders":             buy_orders,
        "sell_count":             len(sell_orders),
        "buy_count":              len(buy_orders),
        "executed_orders":        executed_orders,
        "skipped_band":           skipped_band,
        "reinvest_orders":        reinvest_orders,
        "buys_skipped_unsettled": buys_skipped_unsettled,
        "sell_settled":           settled,
        "name_map":               name_map,
        "targets":                targets,
    }