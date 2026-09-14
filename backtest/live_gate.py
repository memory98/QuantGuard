#!/usr/bin/env python3
"""
backtest/live_gate.py — 실계좌 계속/중단 게이트 (AUDIT.md ③ STEP E)
====================================================================
STEP B(신호가 죽었나)·STEP D(가드를 바꿀까)와 **다른 질문**에 답한다:
**"실계좌에 자본을 계속 태울 것인가."**

이 질문에는 2026-09-08까지 사전 기준이 없었다. 기준 없이 손실 구간을 지나면
판단이 그때의 손실 크기에 끌려간다 — [[signal-tuning-freeze]]가 기록한 실패 경로다.

**왜 손실 크기를 기준으로 쓰지 않는가.**
주간 상대수익률은 표준편차 4.67%p / 평균 −0.72%p(11구간 실측)라 t=−0.51이다.
이 노이즈에서 "열위"를 t=2로 확정하려면 약 169주가 필요하다. 즉 −23%든 −30%든
**수익률은 26주 안에 아무것도 말해주지 않는다.** 그래서 STEP E의 중단선은 전부
수익률이 아니라 **"시스템이 깨졌다"는 구조적 증거**로만 구성한다.

관측 전용이다. 매매 로직도, 신호 파라미터도 건드리지 않는다.
출력은 `signal_quality_ledger.json`의 `live_gate` 블록이며 주간 텔레그램이 렌더한다.

실행: python backtest/live_gate.py   (CI에서 signal_quality.py 직후)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "signal_quality_ledger.json"
ARCHIVE_DIR = ROOT / "data" / "s3_archive" / "latest_signal"

# ── STEP E 기준 (사전 고정 2026-09-08 — 사후에 바꾸지 않는다) ──
# 관측 창은 STEP B와 같은 시계를 쓴다(신호품질 원장의 첫 구간부터). 새 날짜를
# 고르면 그 자체가 자의적 선택이 되고, 두 기준의 주차가 어긋나 해석이 갈린다.
HORIZON_WEEKS = 26              # 이 주차에 도달하면 STEP B 판정으로 넘긴다
MIDPOINT_WEEKS = 13             # 절반 시점 — 여기서 이미 망가졌으면 26주를 기다리지 않는다
MIDPOINT_SPREAD_FLOOR_PCT = -10.0   # top10−유니버스 누적이 이 아래면 조기 중단
PROXY_CORR_FLOOR = 0.80         # signal_quality와 동일 상수(대리지표 가정 붕괴선)
PROXY_BREACH_STREAK = 2         # 1주는 결측·이벤트일 수 있다. 2주 연속이면 구조적.


class ExecutionIntegrityCheck:
    """실행 무결성 — 집행이 설계대로 끝났는가만 본다(수익률과 무관).

    집행을 못 믿으면 26주를 채워도 그 표본 자체가 쓰레기가 된다. 그래서 이 위반은
    수익률이 아무리 좋아도 즉시 중단 사유다.

    위반으로 세는 것(모두 fail-safe 방향):
      - `executed_orders[].ok`가 거짓 — 주문이 거부/실패
      - `buys_skipped_unsettled` 참 — 매도 미결제로 매수를 건너뛴 반쪽 리밸런싱
      - `total_equity_checked` 결측/0 이하 — 잔고 조회 실패(폴백 오염 클래스)
      - `execution_audit.ok` 거짓 — 체결 감사가 불일치를 보고

    위반으로 세지 **않는** 것(정상 동작이라 오탐이 된다):
      - 주문 0건(BEAR 현금 관망 주)
      - `skipped_band` 비어있지 않음(밴드 내 유지는 설계된 스킵)
      - `force_test_mode=True` 기록(콘솔 테스트는 실전 표본이 아니다)
    """

    def __init__(self, archive_dir: Path = ARCHIVE_DIR):
        self.archive_dir = archive_dir

    def scan(self, since: str | None) -> list[dict]:
        """since(YYYY-MM-DD) 이후 실전 스냅샷의 위반 목록. 아카이브가 없으면 빈 목록.

        아카이브 부재를 위반으로 세지 않는다 — 로컬 실행 등 정당한 부재가 있고,
        없는 파일을 근거로 실계좌를 멈추면 그게 더 위험한 오작동이다.
        """
        if not self.archive_dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self.archive_dir.glob("*.json")):
            day = path.stem
            if since and day < since:
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                out.append({"date": day, "kind": "ARCHIVE_CORRUPT",
                            "detail": "스냅샷 JSON 파손"})
                continue
            if d.get("force_test_mode"):
                continue
            out.extend(self._violations(day, d))
        return out

    def _violations(self, day: str, d: dict) -> list[dict]:
        out = []
        korea = d.get("korea") or {}

        equity = d.get("total_equity_checked")
        if not isinstance(equity, (int, float)) or equity <= 0:
            out.append({"date": day, "kind": "EQUITY_MISSING",
                        "detail": f"total_equity_checked={equity!r}"})

        failed = [e for e in (korea.get("executed_orders") or [])
                  if isinstance(e, dict) and not e.get("ok")]
        if failed:
            out.append({"date": day, "kind": "ORDER_FAILED",
                        "detail": f"{len(failed)}건 실패 "
                                  f"({', '.join(str(e.get('code')) for e in failed[:5])})"})

        if korea.get("buys_skipped_unsettled"):
            out.append({"date": day, "kind": "PARTIAL_REBALANCE",
                        "detail": "매도 미결제로 매수 스킵(반쪽 리밸런싱)"})

        audit = korea.get("execution_audit")
        if isinstance(audit, dict) and audit.get("ok") is False:
            out.append({"date": day, "kind": "EXEC_AUDIT_MISMATCH",
                        "detail": str(audit.get("reason", ""))[:120]})
        return out


class LiveTradingGate:
    """STEP E 판정. 우선순위: 실행 > 대리지표 > 신호 조기사망 > 지평 도달.

    앞의 것이 걸리면 뒤는 보지 않는다 — 집행이 깨진 상태의 신호 지표는
    해석할 가치가 없기 때문이다.
    """

    def __init__(self, ledger: dict, checker: ExecutionIntegrityCheck | None = None):
        self.ledger = ledger or {}
        self.records = [r for r in self.ledger.get("records", [])
                        if r.get("ic") is not None]
        self.checker = checker or ExecutionIntegrityCheck()

    # ── 관측치 ──
    def window_start(self) -> str | None:
        """STEP B와 같은 시계를 쓴다 — 신호품질 원장의 첫 구간 시작일."""
        recs = self.ledger.get("records") or []
        return recs[0].get("from") if recs else None

    def spread_cum_pct(self) -> float:
        return round(sum(r.get("spread_pct") or 0.0 for r in self.records), 3)

    def proxy_breach_streak(self) -> int:
        """최근부터 연속으로 상관이 바닥 아래인 주 수. 결측은 연속을 끊는다."""
        streak = 0
        for r in reversed(self.ledger.get("records", [])):
            c = r.get("proxy_corr")
            if c is None or c >= PROXY_CORR_FLOOR:
                break
            streak += 1
        return streak

    def eta(self) -> str | None:
        """26주 도달 예상일(마지막 구간 종료일 + 남은 주). 표본 없으면 None."""
        recs = self.ledger.get("records") or []
        if not recs:
            return None
        try:
            last = datetime.strptime(recs[-1]["to"], "%Y-%m-%d")
        except (KeyError, ValueError):
            return None
        remain = max(0, HORIZON_WEEKS - len(self.records))
        return (last + timedelta(weeks=remain)).strftime("%Y-%m-%d")

    # ── 판정 ──
    def verdict(self) -> dict:
        weeks = len(self.records)
        violations = self.checker.scan(self.window_start())
        streak = self.proxy_breach_streak()
        spread = self.spread_cum_pct()
        last_corr = next((r.get("proxy_corr")
                          for r in reversed(self.ledger.get("records", []))
                          if r.get("proxy_corr") is not None), None)

        base = {
            "weeks": weeks,
            "horizon_weeks": HORIZON_WEEKS,
            "need": max(0, HORIZON_WEEKS - weeks),
            "eta": self.eta(),
            "window_start": self.window_start(),
            "execution": {"violations": violations},
            "proxy": {"last_corr": last_corr, "floor": PROXY_CORR_FLOOR,
                      "breach_streak": streak, "need_streak": PROXY_BREACH_STREAK},
            "midpoint": {"weeks": MIDPOINT_WEEKS,
                         "reached": weeks >= MIDPOINT_WEEKS,
                         "spread_cum_pct": spread,
                         "floor_pct": MIDPOINT_SPREAD_FLOOR_PCT},
            "criteria": self.criteria(),
        }

        if violations:
            kinds = ", ".join(sorted({v["kind"] for v in violations}))
            return {**base, "status": "STOP_EXECUTION",
                    "note": f"실행 무결성 위반({kinds}) — 즉시 종이매매 전환. "
                            f"집행을 못 믿으면 누적 표본도 못 믿는다"}

        if streak >= PROXY_BREACH_STREAK:
            return {**base, "status": "STOP_PROXY",
                    "note": f"대리지표 상관 {streak}주 연속 < {PROXY_CORR_FLOOR} — "
                            f"DD가드의 전제(KODEX200이 포트 위험을 대변)가 깨졌다"}

        if weeks >= MIDPOINT_WEEKS and spread < MIDPOINT_SPREAD_FLOOR_PCT:
            return {**base, "status": "STOP_SIGNAL_EARLY",
                    "note": f"절반 시점 스프레드 누적 {spread:+.2f}%p "
                            f"< {MIDPOINT_SPREAD_FLOOR_PCT}%p — 26주를 기다릴 값어치가 없다"}

        if weeks >= HORIZON_WEEKS:
            return {**base, "status": "HORIZON_REACHED",
                    "note": "26주 도달 — STEP B 판정과 누적 상대수익률로 최종 결정(STEP E ④)"}

        eta_note = f" (예상 {base['eta']})" if base["eta"] else ""
        return {**base, "status": "RUNNING",
                "note": f"중단선 미발동 — 계속. {base['need']}주 남음{eta_note}"}

    @staticmethod
    def criteria() -> dict:
        return {
            "horizon_weeks": HORIZON_WEEKS,
            "midpoint_weeks": MIDPOINT_WEEKS,
            "midpoint_spread_floor_pct": MIDPOINT_SPREAD_FLOOR_PCT,
            "proxy_corr_floor": PROXY_CORR_FLOOR,
            "proxy_breach_streak": PROXY_BREACH_STREAK,
            "note": "사전 고정 2026-09-08 — 사후에 바꾸지 않는다(AUDIT.md ③ STEP E)",
        }

    def alerts(self) -> list[str]:
        """정상이면 침묵. 중단선 발동만 알린다."""
        v = self.verdict()
        if v["status"] in ("RUNNING", "HORIZON_REACHED"):
            return []
        return [f"🛑 STEP E 중단선 발동: {v['status']} — {v['note']}"]


def main() -> int:
    if not LEDGER_PATH.exists():
        print("⚠️ 신호품질 원장 없음 → STEP E 판정 스킵(다음 주부터)")
        return 0
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("⚠️ 신호품질 원장 파손 → STEP E 판정 스킵")
        return 0

    gate = LiveTradingGate(data)
    v = gate.verdict()
    data["live_gate"] = v
    LEDGER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print(f"🚦 STEP E 실계좌 게이트: {v['status']} — {v['note']}")
    print(f"   진행 {v['weeks']}/{HORIZON_WEEKS}주"
          + (f" (예상 {v['eta']})" if v["eta"] else ""))
    print(f"   ① 실행 무결성 : 위반 {len(v['execution']['violations'])}건"
          f" (창 시작 {v['window_start']})")
    for x in v["execution"]["violations"][:5]:
        print(f"      - {x['date']} {x['kind']}: {x['detail']}")
    p = v["proxy"]
    print(f"   ② 대리지표 상관: {p['last_corr']} (바닥 {p['floor']}, "
          f"연속 {p['breach_streak']}/{p['need_streak']}주)")
    m = v["midpoint"]
    print(f"   ③ 신호 조기사망: 스프레드 누적 {m['spread_cum_pct']:+.2f}%p "
          f"(바닥 {m['floor_pct']}%p, {m['weeks']}주 시점"
          f"{'' if m['reached'] else ' — 아직 미도달'})")
    for a in gate.alerts():
        print(f"\n{a}")
    print(f"\n💾 원장 갱신: {LEDGER_PATH.relative_to(ROOT)} (live_gate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
