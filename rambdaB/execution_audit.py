"""
execution_audit.py — 주문 '접수'와 '체결'을 분리 관측하는 읽기 전용 감사 모듈
================================================================
왜 필요한가 (AUDIT #OPEN-1):
  지금까지 latest_signal의 executed_orders[].ok 는 **주문 접수 성공(rt_cd=0)**만
  의미했다. 실제로 몇 주가 어떤 가격에 체결됐는지는 어디에도 남지 않는다.
  그 결과 주간 성과가 나빴을 때 "가드 판단이 틀린 것"과 "체결이 나쁜 것"을
  원리적으로 구분할 수 없었다(2026-08-22 세션에서 실제로 막힌 지점).

무엇을 하나:
  주문 집행이 모두 끝난 뒤 잔고를 한 번 더 읽어, 주문 전 보유수량과 비교해
  **실제 체결 수량**을 확정하고 매입평균가를 함께 기록한다.

무엇을 하지 않나 (중요):
  - 매매 판단에 일절 관여하지 않는다. 주문을 내지도, 취소하지도, 예산을 바꾸지도 않는다.
  - 어떤 실패도 위로 던지지 않는다(fail-safe). 감사가 실패하면 감사 결과만 비고,
    리밸런싱 자체는 그대로 성공 처리된다. 관측 장치가 매매를 죽이면 안 된다.

스키마 신뢰도 (CLAUDE.md 외부 API 목킹 원칙):
  - hldg_qty / pdno : **검증됨**. fix17에서 실사고(hldn_qty 오타)로 교정됐고
    dashboard/app.py 가 동일 필드로 같은 API를 쓰고 있다.
  - pchs_avg_pric   : **미검증**. 저장소 어디에도 사용처가 없고 docs/에 실캡처도 없다.
    → 값이 없으면 조용히 None. 이 필드에 의존하는 로직은 만들지 않는다.
    → 대신 output1 의 실제 키 목록을 함께 기록해, 다음 실전 실행 아카이브를 보면
      추측 없이 진짜 필드명을 알 수 있게 한다(스키마 자가 검증, #OPEN-V).
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import urllib3

from config import FORCE_TEST_MODE, EXEC_AUDIT_ENABLED

# 매입평균가 후보 필드. 첫 번째가 KIS 공식 문서상 이름이나 저장소 내 검증 사례가
# 없으므로 '추정'이다. 순서대로 찾아보고 없으면 None (절대 예외를 던지지 않는다).
AVG_PRICE_FIELD_CANDIDATES = ("pchs_avg_pric", "pchs_avg_prc", "avg_prvs")


class ExecutionAuditor:
    """주문 전/후 보유수량을 대조해 실제 체결을 관측한다. 읽기 전용."""

    def __init__(self, token: str, balance_spec_fn,
                 poll_interval: float = 5.0, max_polls: int = 2):
        """
        - balance_spec_fn: () -> (url, headers). 잔고조회 요청 스펙의 단일 소스를
          주입받는다. 여기서 URL/헤더를 다시 만들면 fix23이 없앤 '중복 정의로 인한
          수동 동기화 버그'를 되살리게 된다.
        - poll_interval / max_polls: 체결 반영 지연 대비 재조회. 상한이 있어
          Lambda 실행시간을 잠식하지 않는다.
        """
        self._token = token
        self._balance_spec_fn = balance_spec_fn
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self._before: dict[str, int] = {}

    # ── 주문 전 ────────────────────────────────────────────
    def capture_before(self, holdings: dict) -> None:
        """리밸런싱이 이미 조회해 둔 보유 dict를 그대로 받는다(추가 API 호출 없음)."""
        try:
            self._before = {c: int(v.get("qty", 0)) for c, v in (holdings or {}).items()}
        except Exception:
            self._before = {}

    # ── 주문 후 ────────────────────────────────────────────
    def _read_balance_raw(self) -> list:
        """잔고 output1 원본 리스트. 실패하면 빈 리스트(예외 전파 없음)."""
        url, headers = self._balance_spec_fn()
        http = urllib3.PoolManager()
        res = http.request("GET", url, headers=headers)
        data = json.loads(res.data.decode("utf-8"))
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"rt_cd={data.get('rt_cd')} msg1={data.get('msg1', '')}")
        return data.get("output1", []) or []

    @staticmethod
    def _pick_avg_price(item: dict):
        """매입평균가 best-effort 추출. 후보 필드가 다 없으면 (None, None)."""
        for field in AVG_PRICE_FIELD_CANDIDATES:
            raw = item.get(field)
            if raw not in (None, "", "0"):
                try:
                    return int(float(raw)), field
                except (TypeError, ValueError):
                    continue
        return None, None

    def audit(self, executed_orders: list) -> dict:
        """체결 관측 리포트. 어떤 상황에서도 dict를 돌려주고 예외를 던지지 않는다."""
        report = {
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False,
            "reason": None,
            "orders": [],
            "avg_price_field": None,          # 실제로 값을 찾은 필드명 (None=못 찾음)
            "avg_price_schema_verified": False,  # 항상 False — 실캡처 대조 전까지
            "balance_output1_keys": [],       # 스키마 자가검증용 (#OPEN-V)
        }
        if not executed_orders:
            report["ok"] = True
            report["reason"] = "NO_ORDERS"
            return report

        items = []
        last_err = ""
        for attempt in range(1, self._max_polls + 1):
            time.sleep(self._poll_interval)
            try:
                items = self._read_balance_raw()
                break
            except Exception as e:               # 통신/응답 이상 — 감사만 포기
                last_err = str(e)
                print(f"⚠️ [체결감사] 잔고 재조회 실패 ({attempt}/{self._max_polls}): {e}")
        else:
            report["reason"] = f"BALANCE_READ_FAILED: {last_err}"
            print("⚠️ [체결감사] 실패 → 감사 결과 없이 진행 (매매에는 영향 없음)")
            return report

        after: dict[str, int] = {}
        avg: dict[str, int] = {}
        for item in items:
            try:
                code = item.get("pdno", "")          # 검증된 필드
                qty = int(item.get("hldg_qty", 0))   # 검증된 필드 (fix17)
            except (TypeError, ValueError):
                continue
            if not code:
                continue
            after[code] = qty
            price, field = self._pick_avg_price(item)
            if price is not None:
                avg[code] = price
                report["avg_price_field"] = report["avg_price_field"] or field
        if items:
            report["balance_output1_keys"] = sorted(items[0].keys())

        # 주문을 종목·방향별로 합산해 요청수량 집계 (재투입 주문 포함)
        requested: dict[tuple, int] = {}
        limits: dict[tuple, int] = {}
        for o in executed_orders:
            if not o.get("ok"):
                continue                      # 접수 자체가 실패한 건 체결 대상이 아님
            key = (o.get("code", ""), o.get("side", ""))
            requested[key] = requested.get(key, 0) + int(o.get("qty", 0))
            limits[key] = o.get("limit_price", 0)

        for (code, side), req_qty in sorted(requested.items()):
            before_q = self._before.get(code, 0)
            after_q = after.get(code, 0)
            delta = after_q - before_q
            filled = delta if side == "BUY" else -delta
            filled = max(0, min(filled, req_qty))     # 관측 잡음 방어
            if filled >= req_qty:
                status = "FULL"
            elif filled <= 0:
                status = "NONE"
            else:
                status = "PARTIAL"
            report["orders"].append({
                "code": code,
                "side": side,
                "req_qty": req_qty,
                "filled_qty": filled,
                "fill_status": status,
                "limit_price": limits.get((code, side), 0),
                "qty_before": before_q,
                "qty_after": after_q,
                # 신규 진입이면 매입평균가 = 이번 체결가. 기존 보유분이 있으면 혼합값이라
                # 이번 주문의 체결가로 해석하면 안 된다.
                "avg_price": avg.get(code),
                "avg_price_blended": before_q > 0,
            })

        report["ok"] = True
        n_full = sum(1 for o in report["orders"] if o["fill_status"] == "FULL")
        n_part = sum(1 for o in report["orders"] if o["fill_status"] == "PARTIAL")
        n_none = sum(1 for o in report["orders"] if o["fill_status"] == "NONE")
        print(f"🔎 [체결감사] 전량체결 {n_full}건 / 부분체결 {n_part}건 / 미체결 {n_none}건"
              + (f" · 매입평균가 필드={report['avg_price_field']}(미검증)"
                 if report["avg_price_field"] else " · 매입평균가 필드 없음"))
        return report


def run_execution_audit(token: str, balance_spec_fn, holdings_before: dict,
                        executed_orders: list, poll_interval: float = 5.0) -> dict:
    """korea.py에서 부르는 진입점. 어떤 예외도 밖으로 내보내지 않는다."""
    if not EXEC_AUDIT_ENABLED:
        # [fix34] 기본 OFF. 잔고 재조회도 대기도 하지 않으므로 fix33 이전과
        # 실행 경로가 완전히 동일하다. 켜려면 Lambda 환경변수 EXEC_AUDIT_ENABLED=true.
        return {"ok": True, "reason": "DISABLED", "orders": []}
    if FORCE_TEST_MODE:
        # 모의 모드에서는 주문이 실제로 나가지 않아 잔고가 변하지 않는다. 그대로 감사하면
        # 전 종목을 '미체결'로 기록해 아카이브에 가짜 신호를 남긴다 → 아예 건너뛴다.
        print("🧪 [체결감사] FORCE_TEST_MODE — 실주문이 없으므로 감사 건너뜀")
        return {"ok": True, "reason": "FORCE_TEST_MODE", "orders": []}
    try:
        auditor = ExecutionAuditor(token, balance_spec_fn, poll_interval=poll_interval)
        auditor.capture_before(holdings_before)
        return auditor.audit(executed_orders)
    except Exception as e:                      # 관측 장치가 매매를 죽이면 안 된다
        print(f"⚠️ [체결감사] 예기치 못한 오류 → 건너뜀: {e}")
        return {"ok": False, "reason": f"UNEXPECTED: {e}", "orders": []}
