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

LEDGER = Path(__file__).resolve().parent.parent / "data" / "shadow_ledger.json"


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
    lines.append(f"• 실계좌: {d.get('account_pct', 0):+.2f}%")
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
