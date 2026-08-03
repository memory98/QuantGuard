"""
kis_common.py — Lambda B 공통 KIS 함수 (중복 제거, 단일 소스)
========================================================
기존엔 korea.py와 lambda_function.py에 get_tick_size/calc_limit_price/execute_order가
각각 정의돼 수동 동기화 중이었다(주석: "korea.py fix13과 동일하게 교정"). 한쪽만
고치면 잠복 버그가 되므로 여기로 통합한다. execute_order는 korea의 bool 계약으로 통일
(usa.py는 뼈대라 실집행 없음 → 영향 없음).
"""
from __future__ import annotations

import json

import urllib3

from config import (
    KIS_APPKEY, KIS_APPSECRET, KIS_ACCOUNT, KIS_PRDT_CODE,
    URL_BASE, FORCE_TEST_MODE,
)


def get_tick_size(price: float) -> int:
    """ETF 호가 단위: 가격대 무관 5원 고정."""
    return 5


def calc_limit_price(current_price: float, rate: float = -0.01) -> int:
    """지정가 = 현재가*(1+rate)를 5원 틱으로 반올림. 매수(rate>=0)는 한 틱 올림(체결 확보)."""
    raw_price = current_price * (1 + rate)
    tick      = get_tick_size(raw_price)
    if rate >= 0:
        limit_price = (int(raw_price // tick) + 1) * tick
    else:
        limit_price = int(raw_price // tick) * tick
    return max(limit_price, tick)


def execute_order(token: str, code: str, qty: int,
                  is_buy: bool, limit_price: int = 0) -> bool:
    """국내 현물 현금 주문(order-cash). 접수 성공(rt_cd=0)이면 True.

    [fix13] TR_ID: TTTC0802U(매수)/TTTC0801U(매도), custtype=P 필수.
    ORD_DVSN: 00(지정가)/01(시장가). limit_price>0이면 지정가.
    """
    if qty <= 0:
        return False

    label      = "매수" if is_buy else "매도"
    order_type = f"지정가({limit_price:,}원)" if limit_price > 0 else "시장가"

    if FORCE_TEST_MODE:
        print(f"🧪 [테스트 모드 주문 성공 시뮬레이션] "
              f"[{label} {order_type}] {code} {qty}주 — 실제 주문 미전송")
        return True

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
        print(f"❌ [{label} 실패] {code}: {res_data.get('msg1', '')}")
    except Exception as e:
        print(f"❌ 주문 전송 에러 ({code}): {e}")
    return False
