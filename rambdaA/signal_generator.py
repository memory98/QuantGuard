"""
signal_generator.py — Lambda A: 시그널 생성기
========================================================
실행: 매주 월요일 15:05 KST
EventBridge: 타임존 Asia/Seoul / Cron: 5 15 ? * MON *
Lambda 설정: Timeout 12분 / Memory 512MB

[수정 이력]
  fix7: 신규 상장 ETF 모멘텀 오염 방어
        - yf.py range=1y 고정이라 상장 후 얼마 안된 ETF의
          base 가격이 초기 NAV(~10,000~20,000원)를 가리켜
          모멘텀이 300%+ 로 뻥튀기되는 버그 수정
        - 해결: 데이터 길이 체크를 LOOKBACK * 1.5(≈189일) 이상으로 강화
          → 상장 후 9개월 미만 ETF 자동 제외
  fix7: 섹터 키워드 충돌 수정
        - "글로벌HBM반도체" 같은 종목이 반도체가 아닌 미국_기타로
          분류되던 버그 수정 (SECTOR_KEYWORDS 순서 및 키워드 정밀화)
        - 반도체/2차전지 등 국내 섹터를 미국_기타보다 먼저 매칭
  fix8: 불필요한 pip install 제거
        - FinanceDataReader, yfinance 모두 커스텀 fdr.py / yf.py 로 대체됨
        - 외부 패키지 설치 불필요 → 첫 줄 subprocess pip install 삭제
  fix9: 모멘텀 계산 방식 날짜 기반으로 교체 (yf.py range=2y 연동)
        - iloc[-LOOKBACK] → base_date 이후 첫 거래일 종가로 변경
        - 신규 상장 ETF 방어: base 기준일 근방 데이터 없으면 제외
        - yf.py range=1y → range=2y 변경으로 데이터 충분히 확보
"""
import json
import os
import boto3
import time as _time
import pandas as pd
import fdr
import yf
import urllib3
from datetime import datetime, timedelta
from config import (
    S3_BUCKET_NAME, QUANT_SIGNAL_KEY,
    KIS_APPKEY, KIS_APPSECRET, KIS_ACCOUNT, KIS_PRDT_CODE,
    URL_BASE, VIX_THRESHOLD,
)

# ── 설정 ────────────────────────────────────────────────────
NUM_TARGETS   = 10     # 최종 매수 종목 수
VOLUME_CUTOFF = 100    # 거래대금 컷오프 (상위 N개)
LOOKBACK      = 126    # 모멘텀 계산 기간 (영업일, 약 6개월)

# [fix7] 신규 상장 ETF 오염 방어
# LOOKBACK(126) * 1.5 = 189일치 데이터 미만인 종목은 제외
# → 상장 후 약 9개월 미만 ETF가 초기 NAV를 base로 잡는 버그 차단
MIN_DATA_LENGTH = int(LOOKBACK * 1.5)   # 189

# 제거할 ETF 키워드 (레버리지/인버스/채권/환율 등)
EXCLUDE_KEYWORDS = [
    '레버리지', '인버스', '곱버스', '2X', '-1X', '-2X',
    '국채', '채권', '단기', '머니마켓', 'MMF',
    '달러', '엔화', '유로', '위안',
    '선물H', '(H)',
    # ── 국내 ETF 전용 운용: 해외 ETF 완전 차단 ──────────────
    '미국', '나스닥', 'S&P', '글로벌', '선진국', 'MSCI',
    '아시아', '신흥국', '이머징', '필라델피아',
]

# [fix7] 섹터 분류 키워드 사전 — 순서가 우선순위
# 규칙: 구체적인 섹터(반도체, 2차전지 등)를 포괄적인 섹터(미국_기타, 글로벌) 보다 앞에 배치
# "글로벌HBM반도체" → 미국_기타("글로벌") 에 걸리던 버그 수정
#   → 반도체 섹터를 미국_기타보다 앞에 두면 반도체로 올바르게 분류됨
SECTOR_KEYWORDS = [
    # ── 국내 섹터 (구체적인 것 먼저) ───────────────────────
    ("반도체",          ["반도체", "AI반도체"]),                     # fix7: "글로벌반도체" 제거 → 아래 별도 처리
    ("2차전지",         ["2차전지", "배터리", "리튬"]),
    ("바이오_헬스케어", ["헬스케어", "바이오", "제약"]),
    ("IT_소프트웨어",   ["IT", "소프트웨어", "인터넷", "테크"]),     # fix7: "AI" 제거 → AI반도체가 반도체로 가야 함
    ("자동차_모빌리티", ["자동차", "모빌리티", "전기차"]),
    ("에너지_화학",     ["에너지", "화학", "석유", "정유"]),
    ("금융_은행",       ["은행", "금융", "보험", "증권"]),
    ("건설_부동산",     ["건설", "부동산", "리츠", "인프라"]),
    ("소비_유통",       ["화장품", "소비", "유통", "의류", "패션"]),
    ("철강_소재",       ["철강", "소재", "금속", "화학소재"]),
    ("중공업_방산",     ["중공업", "방산", "조선", "기계"]),
    ("통신_미디어",     ["통신", "미디어", "방송", "엔터"]),
    ("원자재_금",       ["골드", "원자재", "농산물"]),               # fix7: "금" 제거 → "중공업" "금융" 등에서 오매칭 방지
    ("국내_대형지수",   ["KOSPI200", "코스피200"]),                  # fix7: "200" 단독 키워드 제거 → 오매칭 방지
    ("국내_중소형",     ["코스닥150", "중소형", "스몰캡"]),
    ("국내_가치",       ["밸류업", "가치", "배당"]),
    # ── 해외/글로벌 섹터 제거 (국내 ETF 전용 운용) ──────────
    # EXCLUDE_KEYWORDS에서 미국/글로벌 ETF를 유니버스 진입 단계에서 차단
    ("기타",            []),
]


# ============================================================
# 1. KIS API 영업일 확인
# ============================================================

def is_business_day(token: str) -> bool:
    """KIS API '국내휴장일조회'로 오늘이 영업일인지 확인"""
    http  = urllib3.PoolManager()
    today = datetime.today().strftime("%Y%m%d")
    url   = f"{URL_BASE}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APPKEY,
        "appsecret":     KIS_APPSECRET,
        "tr_id":         "CTCA0903R",
    }
    params = f"?BASS_DT={today}&CTX_AREA_NK=&CTX_AREA_FK="
    try:
        _time.sleep(0.5)
        res      = http.request("GET", url + params, headers=headers)
        res_data = json.loads(res.data.decode("utf-8"))
        items    = res_data.get("output", [])
        for item in items:
            if item.get("bass_dt") == today:
                return item.get("opnd_yn", "N") == "Y"
        return True
    except Exception as e:
        print(f"⚠️ 영업일 확인 실패 ({e}) → 영업일로 간주하고 계속 진행")
        return True


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
            raise Exception(f"토큰 발급 실패: {res_data.get('error_description')}")
        return token
    except Exception as e:
        if "Rate Exceeded" in str(e) or "429" in str(e):
            print("⚠️ API Rate Limit 감지됨. 3초 대기 후 재시도합니다...")
            _time.sleep(3)
            return get_access_token()
        raise e


# ============================================================
# 2. ETF 목록 수집 및 필터링
# ============================================================

def get_filtered_etf_list() -> pd.DataFrame:
    """KRX ETF 전체 수집 → 필터링 → 거래대금 상위 100개 반환"""
    print("📋 KRX ETF 전체 목록 수집 중...")
    try:
        etf_list = fdr.StockListing('ETF/KR')
    except Exception as e:
        raise Exception(f"ETF 목록 수집 실패: {e}")

    print(f"   전체 ETF: {len(etf_list)}개")

    etf_list.columns = [c.strip() for c in etf_list.columns]
    name_col   = next((c for c in etf_list.columns if '이름' in c or 'Name' in c), None)
    code_col   = next((c for c in etf_list.columns if '코드' in c or 'Code' in c or 'Symbol' in c), None)
    volume_col = next((c for c in etf_list.columns if '거래' in c or 'Volume' in c), None)

    if not all([name_col, code_col]):
        raise Exception(f"컬럼 파싱 실패. 컬럼 목록: {list(etf_list.columns)}")

    etf_list = etf_list.rename(columns={
        name_col:   "Name",
        code_col:   "Code",
        volume_col: "Volume" if volume_col else "Volume",
    })

    etf_list["Code"] = etf_list["Code"].astype(str).str.zfill(6)

    mask = etf_list["Name"].apply(
        lambda name: not any(kw in str(name) for kw in EXCLUDE_KEYWORDS)
    )
    filtered = etf_list[mask].copy()
    print(f"   필터링 후: {len(filtered)}개 (레버리지/인버스/채권/달러 제거)")

    if "Volume" in filtered.columns:
        filtered["Volume"] = pd.to_numeric(filtered["Volume"], errors="coerce").fillna(0)
        filtered = filtered.nlargest(VOLUME_CUTOFF, "Volume")
    else:
        filtered = filtered.head(VOLUME_CUTOFF)

    print(f"   거래대금 상위 {VOLUME_CUTOFF}개 선정 완료")

    return filtered[["Code", "Name"]].reset_index(drop=True)


# ============================================================
# 3. 섹터 분류
# ============================================================

def classify_sector(name: str) -> str:
    for sector, keywords in SECTOR_KEYWORDS:
        if sector == "기타":
            continue
        if any(kw in name for kw in keywords):
            return sector
    return f"기타_{name}"


# ============================================================
# 4. 모멘텀 계산
# ============================================================

def calc_momentum_scores(etf_df: pd.DataFrame, last_friday: datetime) -> list:
    """
    126 영업일 모멘텀 계산

    [fix9] iloc[-LOOKBACK] → 날짜 기반 base 계산으로 교체
    - 기존: dropna() 후 배열 뒤에서 126번째 → 실제 날짜 보장 안됨
    - 수정: last_friday 기준 정확히 126 영업일 전 날짜를 계산하고
            그 날짜에 가장 가까운 실제 거래일 종가를 base로 사용
    - yf.py range=2y 로 변경하여 데이터 충분히 확보
    """
    # 126 영업일 ≈ 캘린더 186일, 여유 20일 추가해서 206일 전부터 탐색
    BASE_SEARCH_DAYS = int(LOOKBACK * (7 / 5)) + 20   # ≈ 196
    tickers  = [f"{code}.KS" for code in etf_df["Code"]]
    name_map = dict(zip(etf_df["Code"], etf_df["Name"]))

    # base 날짜: last_friday 기준 약 126 영업일 전 (캘린더 ~186일)
    base_date = last_friday - timedelta(days=BASE_SEARCH_DAYS)

    print(f"\n📡 가격 데이터 수집 중 (기준일: {last_friday.strftime('%Y-%m-%d')})...")
    print(f"   모멘텀 base 기준일 탐색 시작: {base_date.strftime('%Y-%m-%d')} (~{LOOKBACK} 영업일 전)")

    batch_size = 50
    all_close  = pd.DataFrame()

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        print(f"   배치 {i//batch_size+1}: {len(batch)}개 수집 중...")
        try:
            raw = yf.download(
                batch,
                start=base_date,
                end=datetime.today(),
                progress=False,
                timeout=60,
                threads=False,
            )

            if "Close" in raw.columns:
                raw_close = raw["Close"]
            else:
                raw_close = raw

            if hasattr(raw_close.index, 'tz') and raw_close.index.tz is not None:
                raw_close.index = raw_close.index.tz_localize(None)

            raw_close = raw_close[raw_close.index <= pd.Timestamp(last_friday)]

            if isinstance(raw_close, pd.Series):
                raw_close = raw_close.to_frame(name=batch[0])

            all_close = pd.concat([all_close, raw_close], axis=1)

        except Exception as e:
            print(f"   ⚠️ 배치 수집 실패: {e}")
            continue

        _time.sleep(3)

    if all_close.empty:
        return []

    scores       = []
    skipped_new  = 0
    skipped_err  = 0

    for ticker in all_close.columns:
        code = ticker.replace(".KS", "")
        try:
            # None 포함 전체 시리즈 (날짜 인덱스 보존)
            closes_raw = all_close[ticker]

            # [fix9] 신규 상장 방어: base_date 이전 데이터가 없으면 제외
            # base_date 시점에 유효한 가격이 없으면 상장 후 얼마 안된 종목
            base_window = closes_raw[closes_raw.index <= pd.Timestamp(base_date + timedelta(days=30))]
            base_window_valid = base_window.dropna()
            if len(base_window_valid) == 0:
                skipped_new += 1
                print(f"   ⏭ {code} 제외 — base 기준일({base_date.strftime('%Y-%m-%d')}) 근방 데이터 없음 (신규 상장)")
                continue

            # current: last_friday 기준 가장 최근 유효 종가
            current_series = closes_raw[closes_raw.index <= pd.Timestamp(last_friday)].dropna()
            if len(current_series) == 0:
                continue
            current = float(current_series.iloc[-1])

            # [fix9] base: base_date 이후 첫 번째 유효 거래일 종가
            # → 정확히 126 영업일 전 날짜에 가장 가까운 실제 거래일
            base_series = closes_raw[closes_raw.index >= pd.Timestamp(base_date)].dropna()
            if len(base_series) == 0:
                continue
            base = float(base_series.iloc[0])

            if base == 0:
                continue

            momentum = current / base - 1

            # [fix10] 200% 캡 제거 — 실제 급등 종목을 오류로 걸러내는 잘못된 로직 삭제
            # 신규 상장 방어는 base_date 근방 데이터 없으면 제외하는 로직으로 충분

            name   = name_map.get(code, code)
            sector = classify_sector(name)
            scores.append({
                "code":     code,
                "name":     name,
                "price":    round(current, 0),
                "momentum": round(momentum, 6),
                "sector":   sector,
            })
        except Exception:
            continue

    print(f"   신규 상장 제외: {skipped_new}개 / 데이터 오류 제외: {skipped_err}개")
    return sorted(scores, key=lambda x: x["momentum"], reverse=True)


# ============================================================
# 5. 섹터별 1개 제한
# ============================================================

def apply_sector_filter(scores: list, n: int = NUM_TARGETS) -> list:
    seen_sectors = set()
    result       = []

    for s in scores:
        if len(result) >= n:
            break
        sector = s["sector"]
        if sector not in seen_sectors:
            seen_sectors.add(sector)
            result.append(s)

    return result


# ============================================================
# 6. VIX 조회
# ============================================================

def fetch_vix(last_friday: datetime) -> tuple:
    try:
        raw = yf.download(
            "^VIX",
            start=last_friday - timedelta(days=30),
            end=datetime.today(),
            progress=False,
        )
        if "Close" in raw.columns:
            raw_close = raw["Close"]
        else:
            raw_close = raw

        if hasattr(raw_close.index, 'tz') and raw_close.index.tz is not None:
            raw_close.index = raw_close.index.tz_localize(None)
        raw_close = raw_close[raw_close.index <= pd.Timestamp(last_friday)].dropna()
        if len(raw_close) == 0:
            return 15.0, "BULL"
        vix    = float(raw_close.iloc[-1])
        status = "BEAR" if vix > VIX_THRESHOLD else "BULL"
        return round(vix, 2), status
    except Exception as e:
        print(f"⚠️ VIX 수집 실패 ({e}) → BULL 기본값")
        return 15.0, "BULL"


# ============================================================
# 7. Lambda 핸들러
# ============================================================

def lambda_handler(event, context):
    # ── 영업일 확인 ─────────────────────────────────────────
    # force_run=true 이벤트로 호출 시 휴장일 체크 우회 (테스트용)
    # 테스트 이벤트: {"force_run": true}
    # EventBridge 자동 실행은 이벤트가 비어있어 정상 체크됨
    if not event.get("force_run"):
        try:
            token = get_access_token()
            if not is_business_day(token):
                print("⛔ 오늘은 휴장일 → 시그널 생성 없이 종료")
                return {"statusCode": 200, "body": "MARKET_CLOSED"}
        except Exception as e:
            print(f"⚠️ 토큰/영업일 확인 실패 ({e}) → 계속 진행")

    # ── 기준일: 지난주 금요일 ────────────────────────────────
    today          = datetime.today()
    days_to_friday = (today.weekday() - 4) % 7
    last_friday    = today - timedelta(days=days_to_friday)
    print(f"⏳ 기준일: {last_friday.strftime('%Y-%m-%d')} (지난주 금요일 종가)")

    # ── ETF 목록 수집 및 필터링 ──────────────────────────────
    try:
        etf_df = get_filtered_etf_list()
    except Exception as e:
        print(f"❌ ETF 목록 수집 실패: {e}")
        return {"statusCode": 500, "body": "ETF list fetch failed"}

    # ── 모멘텀 계산 ──────────────────────────────────────────
    all_scores = calc_momentum_scores(etf_df, last_friday)
    if not all_scores:
        print("❌ 유효한 종목 없음")
        return {"statusCode": 500, "body": "No valid stocks"}

    print(f"\n📊 모멘텀 계산 완료: {len(all_scores)}개")

    # ── 섹터별 1개 제한 → 상위 10개 ─────────────────────────
    top_stocks = apply_sector_filter(all_scores, NUM_TARGETS)

    print(f"\n✅ 최종 선정 {len(top_stocks)}개:")
    for i, s in enumerate(top_stocks, 1):
        print(f"   {i}. {s['name']}({s['code']}) "
              f"섹터:{s['sector']} 모멘텀:{s['momentum']*100:+.1f}%")

    # ── VIX 조회 ─────────────────────────────────────────────
    vix, market_status = fetch_vix(last_friday)
    print(f"\n🌡  VIX: {vix} → {market_status}")

    # ── S3 업로드 ─────────────────────────────────────────────
    output_data = {
        "updated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": market_status,
        "vix":           vix,
        "top_10_stocks": top_stocks,
    }

    try:
        s3 = boto3.client("s3")
        body = json.dumps(output_data, ensure_ascii=False, indent=2)

        # ① Lambda B용 최신 시그널 (기존 경로, 덮어쓰기)
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=QUANT_SIGNAL_KEY, Body=body)
        print(f"\n✅ S3 업로드 완료: {QUANT_SIGNAL_KEY}")

        # ② 날짜별 아카이브 (이력 보존)
        # 경로: quant_signals/YYYY-MM-DD.json
        date_str     = last_friday.strftime("%Y-%m-%d")
        archive_key  = f"quant_signals/{date_str}.json"
        s3.put_object(Bucket=S3_BUCKET_NAME, Key=archive_key, Body=body)
        print(f"✅ S3 아카이브 완료: {archive_key}")

        return {"statusCode": 200, "body": "Signal Uploaded"}
    except Exception as e:
        print(f"❌ S3 업로드 실패: {e}")
        return {"statusCode": 500, "body": "Upload Failed"}