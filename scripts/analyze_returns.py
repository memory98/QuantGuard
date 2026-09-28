#!/usr/bin/env python3
# analyze_returns.py — S3 시그널 아카이브 기반 상대수익률 분석
# 버전: v1.0.20260929.1
#
# 수정 이력:
#   fix41(2026-09-29, #OPEN-BM): 벤치마크·종목 가격 조회가 '요청한 날짜의 종가'인지 검증하지
#     않고 마지막 유효 종가를 조용히 대체하던 결함 제거. 요청일 바가 없으면 해당 구간을
#     provisional(잠정)으로 표기한다. 사고: 2026-09-28 주간분석에서 40분 간격 두 실행이
#     야후 지연 때문에 벤치 -1.97% / +1.29%로 갈려 상대수익률 부호가 뒤집혔다(3.26%p).
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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rambdaA"))
import yf  # 기존 커스텀 야후 파이낸스 모듈 재사용 (신규 의존성 없음)
from data_guard import validate_prices  # [fix41] 시계열 정합성 검증 단일 소스(rambdaA와 공유)

BENCHMARK_TICKER = "069500"  # KODEX 200 — KOSPI200 추종 ETF, 벤치마크로 사용
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "s3_archive" / "latest_signal"
QUANT_DIR = Path(__file__).resolve().parent.parent / "data" / "s3_archive" / "quant_signals"


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


class PricePoint:
    """[fix41] 시계열 조회 결과 — 값과 '그 값이 어느 날 종가인지'를 함께 들고 다닌다.

    기존 코드는 float만 반환해 호출부가 신선도를 알 수 없었다(#OPEN-BM). 요청일과
    실제 종가일이 다르면 `is_exact`가 False이고, 호출부는 그 구간을 잠정으로 표기한다.
    """

    __slots__ = ("value", "as_of", "requested")

    def __init__(self, value: float, as_of, requested):
        self.value = float(value)
        self.as_of = as_of
        self.requested = requested

    @property
    def is_exact(self) -> bool:
        """요청한 날짜 자체의 종가인가. 아니면 과거 종가로 대체된 것."""
        return self.as_of.date() == self.requested.date()

    @property
    def stale_days(self) -> int:
        return (self.requested.date() - self.as_of.date()).days

    def __repr__(self):
        return f"PricePoint({self.value:,.0f} @{self.as_of.date()} req={self.requested.date()})"


def last_close_on_or_before(series, requested):
    """[fix41] 요청일 이하 마지막 유효 종가를 PricePoint로 반환. 없으면 None.

    벤치마크와 개별 종목이 같은 조회 규칙을 쓰도록 단일 소스로 둔다.
    """
    import pandas as pd
    s = series[series.index <= pd.Timestamp(requested)].dropna()
    if s.empty:
        return None
    return PricePoint(float(s.iloc[-1]), s.index[-1].to_pydatetime(), requested)


class BenchmarkFetcher:
    """KOSPI200 추종 ETF 가격 시계열 조회 (rambdaA/yf.py 재사용)."""

    # 시계열 전체가 이 일수 넘게 낡으면 피드 중단으로 보고 거부(구간별 잠정 판정과는 별개)
    MAX_SERIES_STALE_DAYS = 10

    def __init__(self, ticker: str = BENCHMARK_TICKER):
        self.ticker = ticker
        self._prices = None

    def _load(self, start: datetime, end: datetime):
        df = yf.download(self.ticker, start=start, end=end)
        if df.empty:
            raise RuntimeError(f"벤치마크({self.ticker}) 가격 조회 실패 — 야후 API 응답 없음")
        prices = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
        self._prices = prices
        # [fix41] 시계열 자체의 정합성(행수·비정상값·미래날짜)은 rambdaA와 같은 검증기로.
        # 구간별 신선도는 price_on_or_before의 is_exact가 따로 판정한다.
        valid = prices.dropna()
        if valid.empty:
            raise RuntimeError(f"벤치마크({self.ticker}) 유효 종가 0건 — 야후 응답 이상")
        ok, reason = validate_prices(
            last_date=valid.index[-1].to_pydatetime(), num_rows=len(valid),
            last_value=float(valid.iloc[-1]), as_of=end,
            min_rows=2, max_stale_days=self.MAX_SERIES_STALE_DAYS)
        if not ok:
            raise RuntimeError(f"벤치마크({self.ticker}) 시계열 검증 실패 — {reason}")

    def price_on_or_before(self, date: datetime) -> PricePoint:
        """해당 날짜 이하 가장 최근 거래일 종가를 PricePoint로 반환 (휴장일 보정).

        [fix41] float이 아니라 PricePoint를 돌려준다. 호출부는 `is_exact`로
        '요청일 종가인지, 과거 값으로 대체된 것인지'를 반드시 구분해야 한다.
        """
        if self._prices is None:
            raise RuntimeError("가격 데이터 미로드 — fetch_range() 먼저 호출할 것")
        point = last_close_on_or_before(self._prices, date)
        if point is None:
            raise RuntimeError(f"{date.date()} 이전 벤치마크 가격 없음")
        return point

    def fetch_range(self, start: datetime, end: datetime):
        self._load(start, end)
        return self


class ReturnAnalyzer:
    """포트폴리오 실행 기록 + 벤치마크로 구간별 상대수익률 계산."""

    def __init__(self, runs: list, benchmark: BenchmarkFetcher, net_deposits: dict = None):
        self.runs = runs
        self.benchmark = benchmark
        self.net_deposits = net_deposits or {}
        self._pcache = {}

    def _price_series(self, code):
        if code not in self._pcache:
            try:
                df = yf.download(code, datetime(2026, 1, 1), datetime.today())
                s = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
                if hasattr(s.index, "tz") and s.index.tz is not None:
                    s.index = s.index.tz_localize(None)
                self._pcache[code] = s.dropna()
            except Exception:
                self._pcache[code] = None
        return self._pcache[code]

    def _if_invested(self, entry_dt, exit_dt):
        """엔트리 시점 시그널의 top10을 균등매수했다면의 수익률(가드가 없었을 때 반사실).

        BEAR로 현금 대피한 주에 '샀으면 얼마였나'를 정량화 → 가드가 아낀(또는 놓친) 크기.
        시그널 파일: 엔트리 실행일(월) 직전 금요일 기준 quant_signals/<금요일>.json 의 top_10_stocks.

        [fix41] (수익률, 신선도미달_종목코드들) 튜플을 반환한다. 벤치마크와 같은 이유로,
        요청일 종가가 아직 없으면 조용히 과거 종가로 계산되던 것을 호출부에 알린다.
        """
        import pandas as pd
        lf = entry_dt - timedelta(days=(entry_dt.weekday() - 4) % 7)
        f = QUANT_DIR / f"{lf:%Y-%m-%d}.json"
        if not f.exists():
            return None, []
        try:
            picks = json.loads(f.read_text(encoding="utf-8")).get("top_10_stocks", [])
        except Exception:
            return None, []
        rets = []
        stale_codes = []       # [fix41] 요청일 종가가 없어 과거 값으로 대체된 종목
        for p in picks:
            s = self._price_series(p["code"])
            if s is None or len(s) == 0:
                continue
            a = last_close_on_or_before(s, entry_dt)
            b = last_close_on_or_before(s, exit_dt)
            if a is None or b is None or a.value <= 0:
                continue
            if not (a.is_exact and b.is_exact):
                stale_codes.append(p["code"])
            rets.append(b.value / a.value - 1)
        if not rets:
            return None, []
        return sum(rets) / len(rets), stale_codes

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
            benchmark_return = bench_curr.value / bench_prev.value - 1

            # [fix41] 요청일 종가가 아니면 이 구간 수치는 확정이 아니다(#OPEN-BM).
            # 조용히 과거 종가로 대체하지 않고 잠정으로 표기해 기록 단계에서 걸러지게 한다.
            reasons = []
            for label, pt in (("기초", bench_prev), ("기말", bench_curr)):
                if not pt.is_exact:
                    reasons.append(f"벤치마크 {label}({pt.requested.date()}) 종가 없음 → "
                                   f"{pt.as_of.date()} 종가로 대체({pt.stale_days}일 전)")

            # [반사실] 이 구간에 top10을 매수했다면(가드 없었을 때)
            if_inv, stale_codes = self._if_invested(prev_dt, curr_dt)
            if stale_codes:
                reasons.append(f"매수했으면 산정: {len(stale_codes)}개 종목이 요청일 종가 없음 "
                               f"({','.join(stale_codes[:3])}{'...' if len(stale_codes) > 3 else ''})")
            # 가드효과 = 실제(현금/보유) - 매수했을때. +면 가드가 손실을 아낌
            guard_effect = (portfolio_return - if_inv) if if_inv is not None else None

            results.append({
                "from": prev["updated_at"],
                "to": curr["updated_at"],
                "portfolio_return_pct": round(portfolio_return * 100, 2),
                "benchmark_return_pct": round(benchmark_return * 100, 2),
                "relative_return_pct": round((portfolio_return - benchmark_return) * 100, 2),
                "if_invested_pct": round(if_inv * 100, 2) if if_inv is not None else None,
                "guard_effect_pct": round(guard_effect * 100, 2) if guard_effect is not None else None,
                "net_deposit": deposit,
                "prev_equity": prev_eq,
                "curr_equity": curr_eq,
                # [fix41] True면 확정치가 아님 — Notion/HISTORY 기록 시 잠정 표기 필수
                "provisional": bool(reasons),
                "provisional_reason": "; ".join(reasons) if reasons else None,
                "benchmark_asof": {"from": bench_prev.as_of.strftime("%Y-%m-%d"),
                                   "to": bench_curr.as_of.strftime("%Y-%m-%d")},
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
    print(f"{'구간':<23} {'포트폴리오':>10} {'벤치마크':>10} {'상대':>9} "
          f"{'매수했으면':>10} {'가드효과':>9}")
    for r in results:
        ii = f"{r['if_invested_pct']:>+9.2f}%" if r.get('if_invested_pct') is not None else f"{'—':>10}"
        ge = f"{r['guard_effect_pct']:>+8.2f}%" if r.get('guard_effect_pct') is not None else f"{'—':>9}"
        mark = "  ⚠️ 잠정" if r.get("provisional") else ""
        print(f"{r['from'][:10]}→{r['to'][:10]:<12} "
              f"{r['portfolio_return_pct']:>+9.2f}% {r['benchmark_return_pct']:>+9.2f}% "
              f"{r['relative_return_pct']:>+8.2f}% {ii} {ge}{mark}")

    # [fix41] 잠정 구간은 확정치가 아니므로 기록 전에 반드시 눈에 띄어야 한다(#OPEN-BM).
    provisional = [r for r in results if r.get("provisional")]
    if provisional:
        print(f"\n⚠️  잠정 구간 {len(provisional)}건 — 확정치 아님. Notion/HISTORY에 기록할 때")
        print("    '잠정'을 명시하고, 종가가 게시된 뒤(보통 다음 거래일) 재실행해 갱신할 것.")
        for r in provisional:
            print(f"    · {r['from'][:10]}→{r['to'][:10]}: {r['provisional_reason']}")

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
