#!/bin/bash
# scripts/run_shadow_weekly.sh — 섀도우 전진검증 주간 자동 실행
# ============================================================
# launchd(매주 월 16:00 KST, Lambda A/B 이후)가 호출한다.
# S3에서 최신 스냅샷을 sync한 뒤 shadow_forward 파이프라인을 실행해
# 원장(data/shadow_ledger.json)을 갱신한다. 로그는 log/shadow_weekly.log.
#
# 수동 실행/테스트: bash scripts/run_shadow_weekly.sh
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
REPO="/Users/jes/Documents/jes-file/Project/stock"
BUCKET="s3://eunsung-quant-guard-bucket"
PROFILE="quantguard-ro"
PY="$REPO/dashboard/.venv/bin/python"

cd "$REPO" || exit 1
mkdir -p log data/s3_archive/universe data/s3_archive/quant_signals data/s3_archive/latest_signal

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 주간 섀도우 실행 시작 ====="
  # universe/는 fix21 이후 생성(2026-08-03~). 아직 없어도 sync는 무해(빈 결과).
  aws s3 sync "$BUCKET/universe/"      data/s3_archive/universe/      --profile "$PROFILE" || echo "⚠️ universe sync 이슈(미생성일 수 있음)"
  aws s3 sync "$BUCKET/quant_signals/" data/s3_archive/quant_signals/ --profile "$PROFILE" || echo "⚠️ quant_signals sync 이슈"
  aws s3 sync "$BUCKET/latest_signal/" data/s3_archive/latest_signal/ --profile "$PROFILE" || echo "⚠️ latest_signal sync 이슈"
  "$PY" backtest/shadow_forward.py || echo "❌ 파이프라인 실행 실패"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 완료 ====="
  echo ""
} >> log/shadow_weekly.log 2>&1
