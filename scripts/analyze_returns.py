#!/usr/bin/env python3
# analyze_returns.py — S3 시그널 아카이브 기반 상대수익률 분석
# 버전: v1.0.20260710.1
#
# 전제: data/s3_archive/latest_signal/*.json 이 `aws s3 sync`로 최신 상태여야 함
#   aws s3 sync s3://eunsung-quant-guard-bucket/latest_signal/ data/s3_archive/latest_signal/ --profile quantguard-ro
#
# 설계 원칙 (TODO 명세 반영):
#   - 이 스크립트는 로컬 계산만 담당한다 (S3 로컬 파싱 + 벤치마크 조회 + 수익률 계산).
#   - Notion 읽기/쓰기(순입출금 조회, 결과 기록)는 Claude Code 세션이 Notion MCP로 직접 수행한다.
#     → 이 스크립트에 Notion API 키를 심지 않는다 (자격증명 최소화 원칙 유지).
#   - net_deposits는 이 스크립트를 호출하는 쪽(Claude)이 Notion '순입출금' 필드를 조회해 인자로 전달한다.
#
# 사용 예:
#   python3 scripts/analyze_returns.py
#   python3 scripts/analyze_returns.py --net-deposits '{"2026-07-06": 0}'

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rambdaA"))
import yf  # 기존 커스텀 야후 파이낸스 모듈 재사용 (신규 의존성 없음)

BENCHMARK_TICKER = "069500"  # KODEX 200 — KOSPI200 추종 ETF, 벤치마크로 사용
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "s3_archive" / "latest_signal"


class SignalArchive:
    """로컬에 동기화된 latest_signal/*.json 아카이브에서 실제 정기 실행 기록만 추출."""

    def __init__(self, archive_dir: Path = ARCHIVE_DIR, exclude_dates: set = None):
        self.archive_dir = archive_dir
        # [분석포함여부] Notion '주간 운영 일지'의 분석포함여부(select) = '제외'인 날짜(YYYY-MM-DD).
        # force_test_mode=False여도 데이터 오염이 확인된 실행(예: 잔고 폴백 버그)을
        # 사람이 수동으로 제외하기 위한 필터 — 자동 판별 불가능한 케이스 대응.
        self.exclude_dates = exclude_dates or set()

    def load_real_runs(self) -> list:
        """force_test_mode=False + total_equity_checked 존재 + 미제외 날짜만, 시간순 정렬."""
        runs = []
        skipped_test = 0
        skipped_excluded = 0
        for path in sorted(self.archive_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("force_test_mode") is not False:
                skipped_test += 1
                continue
            if not data.get("total_equity_checked"):
                continue
            date_key = data["updated_at"][:10]
            if date_key in self.exclude_dates:
                skipped_excluded += 1
                continue
            data["_source_file"] = path.name
            runs.append(data)
        runs.sort(key=lambda d: d["updated_at"])
        if skipped_test:
            print(f"ℹ️  테스트 모드(force_test_mode=True) 기록 {skipped_test}건 제외 "
                  f"(실전 성과 분석 대상 아님)")
        if skipped_excluded:
            print(f"ℹ️  분석포함여부=N 기록 {skipped_excluded}건 제외 "
                  f"(Notion '주간 운영 일지'에서 데이터 오염 등으로 수동 제외)")
        return runs


class BenchmarkFetcher:
    """KOSPI200 추종 ETF 가격 시계열 조회 (rambdaA/yf.py 재사용)."""

    def __init__(self, ticker: str = BENCHMARK_TICKER):
        self.ticker = ticker
        self._prices = None

    def _load(self, start: datetime, end: datetime):
        df = yf.download(self.ticker, start=start, end=end)
        if df.empty:
            raise RuntimeError(f"벤치마크({self.ticker}) 가격 조회 실패 — 야후 API 응답 없음")
        self._prices = df["Close"] if "Close" in df.columns else df.iloc[:, 0]

    def price_on_or_before(self, date: datetime):
        """해당 날짜 이하 가장 최근 거래일 종가 (휴장일 보정)."""
        if self._prices is None:
            raise RuntimeError("가격 데이터 미로드 — fetch_range() 먼저 호출할 것")
        series = self._prices[self._prices.index <= date].dropna()
        if series.empty:
            raise RuntimeError(f"{date.date()} 이전 벤치마크 가격 없음")
        return float(series.iloc[-1])

    def fetch_range(self, start: datetime, end: datetime):
        self._load(start, end)
        return self


class ReturnAnalyzer:
    """포트폴리오 실행 기록 + 벤치마크로 구간별 상대수익률 계산."""

    def __init__(self, runs: list, benchmark: BenchmarkFetcher, net_deposits: dict = None):
        self.runs = runs
        self.benchmark = benchmark
        self.net_deposits = net_deposits or {}

    def compute(self) -> list:
        if len(self.runs) < 2:
            return []

        dates = [datetime.strptime(r["updated_at"][:10], "%Y-%m-%d") for r in self.runs]
        self.benchmark.fetch_range(min(dates), max(dates))

        results = []
        for i in range(1, len(self.runs)):
            prev, curr = self.runs[i - 1], self.runs[i]
            prev_dt = datetime.strptime(prev["updated_at"][:10], "%Y-%m-%d")
            curr_dt = datetime.strptime(curr["updated_at"][:10], "%Y-%m-%d")
            curr_date_key = curr["updated_at"][:10]

            prev_eq = prev["total_equity_checked"]
            curr_eq = curr["total_equity_checked"]
            deposit = self.net_deposits.get(curr_date_key, 0)

            # 순입출금 보정: (기말자산 - 순입출금) / 기초자산 - 1
            portfolio_return = (curr_eq - deposit) / prev_eq - 1

            bench_prev = self.benchmark.price_on_or_before(prev_dt)
            bench_curr = self.benchmark.price_on_or_before(curr_dt)
            benchmark_return = bench_curr / bench_prev - 1

            results.append({
                "from": prev["updated_at"],
                "to": curr["updated_at"],
                "portfolio_return_pct": round(portfolio_return * 100, 2),
                "benchmark_return_pct": round(benchmark_return * 100, 2),
                "relative_return_pct": round((portfolio_return - benchmark_return) * 100, 2),
                "net_deposit": deposit,
                "prev_equity": prev_eq,
                "curr_equity": curr_eq,
            })
        return results


def main():
    parser = argparse.ArgumentParser(description="S3 시그널 아카이브 상대수익률 분석")
    parser.add_argument("--net-deposits", type=str, default="{}",
                        help='JSON: {"YYYY-MM-DD": 순입출금원} — Notion 순입출금 필드에서 채워서 전달')
    parser.add_argument("--exclude-dates", type=str, default="",
                        help='쉼표구분 YYYY-MM-DD 목록 — Notion 분석포함여부=N 날짜를 채워서 전달')
    args = parser.parse_args()
    net_deposits = json.loads(args.net_deposits)
    exclude_dates = {d.strip() for d in args.exclude_dates.split(",") if d.strip()}

    archive = SignalArchive(exclude_dates=exclude_dates)
    runs = archive.load_real_runs()

    print(f"\n📂 실전 정기 실행 기록: {len(runs)}건")
    for r in runs:
        print(f"   {r['updated_at']}  총자산 {r['total_equity_checked']:>12,}원  "
              f"[{r['_source_file']}]")

    if len(runs) < 2:
        print("\n⚠️  구간 수익률 계산에는 최소 2개 이상의 실전 실행 기록이 필요합니다.")
        print("    현재 데이터로는 상대수익률 분석 불가 — 다음 실전 실행 후 재시도하세요.")
        return

    analyzer = ReturnAnalyzer(runs, BenchmarkFetcher(), net_deposits)
    results = analyzer.compute()

    print(f"\n📊 구간별 상대수익률 (vs KODEX 200 / {BENCHMARK_TICKER})")
    print(f"{'구간':<23} {'포트폴리오':>10} {'벤치마크':>10} {'상대수익률':>10} {'순입출금':>12}")
    for r in results:
        print(f"{r['from'][:10]}→{r['to'][:10]:<12} "
              f"{r['portfolio_return_pct']:>+9.2f}% {r['benchmark_return_pct']:>+9.2f}% "
              f"{r['relative_return_pct']:>+9.2f}% {r['net_deposit']:>11,}원")

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
