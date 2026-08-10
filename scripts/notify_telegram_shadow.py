#!/usr/bin/env python3
"""
scripts/notify_telegram_shadow.py — 섀도우 원장 요약을 텔레그램으로 발송
========================================================
shadow_forward가 생성한 data/shadow_ledger.json을 읽어 누적 성적표 요약을
텔레그램으로 쏜다. 토큰/챗ID 환경변수가 없으면 조용히 스킵(워크플로우는 계속 성공).

환경변수: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (GitHub Actions 시크릿에서 주입)
"""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "shadow_ledger.json"
SQ_LEDGER = ROOT / "signal_quality_ledger.json"


def render_signal_quality(sq: dict | None) -> list[str]:
    """신호 품질 요약(#OPEN-S/#OPEN-B). 원장이 없거나 비면 아무 줄도 내지 않는다.

    판정(STEP B)이 INSUFFICIENT인 동안에는 '경향'만 보이고 결론을 내지 않는다.
    """
    if not sq or not sq.get("records"):
        return []
    last = sq["records"][-1]
    v = sq.get("verdict", {})
    lines = ["", "── 신호 품질(순위 예측력) ──"]

    if last.get("ic") is not None:
        bear = " ·BEAR라 미매수" if last.get("market_status") == "BEAR" else ""
        lines.append(f"• 직전주 IC: {last['ic']:+.2f} / "
                     f"top10−유니버스 {last['spread_pct']:+.2f}%p{bear}")
    else:
        lines.append("• 직전주: 계산 불가(가격 결손/표본 부족)")

    if last.get("proxy_corr") is not None:
        lines.append(f"• 가드 대리지표 상관(top10↔KODEX200): {last['proxy_corr']:.2f}")

    status = v.get("status")
    if status == "INSUFFICIENT":
        lines.append(f"• 판정: 보류 — {v.get('weeks', 0)}/{sq.get('criteria', {}).get('min_sample_weeks', 26)}주 "
                     f"(앞으로 {v.get('need', '?')}주)")
    elif status:
        lines.append(f"• 판정: {status} (IC 26주MA {v.get('ic_ma', 0):+.3f})")
    return lines


def render_account(ledger: dict) -> str:
    """실계좌 줄 렌더링. account_pct는 None일 수 있다(채점 가능한 구간 0개).

    기본값 0을 깔면 '데이터 없음'이 '수익 0%'로 둔갑해 조용한 실패가 된다.
    """
    acc = ledger.get("account_pct")
    if acc is None:
        return "• 실계좌: 데이터 없음(채점 구간 0개)"
    return f"• 실계좌: {acc:+.2f}%"


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("ℹ️ 텔레그램 시크릿 없음 → 알림 스킵(정상)")
        return
    if not LEDGER.exists():
        print("⚠️ 원장 없음 → 알림 스킵")
        return

    d = json.loads(LEDGER.read_text(encoding="utf-8"))
    lines = ["📊 섀도우 전진검증 주간 성적표",
             f"기준: {d.get('generated_at', '?')}",
             f"소스: {d.get('source', '?')}",
             "",
             "── 누적 수익률 ──"]
    for name, v in d.get("cumulative", {}).items():
        lines.append(f"• {name}: {v:+.2f}%")
    lines.append(f"• 벤치(KODEX200): {d.get('benchmark_pct', 0):+.2f}%")
    lines.append(render_account(d))

    # 신호 품질(#OPEN-S/#OPEN-B) — 원장이 아직 없으면 아무 줄도 붙지 않는다
    sq = None
    if SQ_LEDGER.exists():
        try:
            sq = json.loads(SQ_LEDGER.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("⚠️ 신호품질 원장 파손 → 해당 섹션 생략")
    lines += render_signal_quality(sq)

    note = d.get("note", "")
    if note:
        lines += ["", note]
    msg = "\n".join(lines)

    data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            print(f"✅ 텔레그램 전송 완료 (HTTP {r.status})")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패({e}) → 워크플로우는 계속 진행")


if __name__ == "__main__":
    main()
