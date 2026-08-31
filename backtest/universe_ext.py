"""
backtest/universe_ext.py — 유니버스 확장 후보 (섀도우 전용, 자본 0)
================================================================
왜:
  2026-06-25~08-31 구간에서 KODEX200 -26.05%, 같은 기간 SPY +4.32% / GLD +9.74%.
  **세계가 나빴던 게 아니라 한국만 무너졌다.** 계좌가 -22% 난 건 종목 선정이 아니라
  자산 100%가 그 한 시장에 있었기 때문이다(신호 자체는 벤치를 +4.19%p 이겼다).
  명세서의 코어-새틀라이트 컨셉("두 엔진을 분리해야 미국 강세/국내 강세 구간에서
  상호 보완")은 있으나 `usa.py`는 35줄 빈 뼈대이고 BUDGET_RATIO=1.0이라 미가동이다.

이 모듈이 시험하는 것:
  해외 주식 API를 새로 붙이는 대신, **국내 상장 해외지수 ETF**를 모멘텀 유니버스에
  넣으면 기존 국내 주문 경로를 그대로 쓰면서 분산이 생기는가?
  현재는 `EXCLUDE_KEYWORDS`의 해외 차단 12개 키워드가 진입 단계에서 막고 있다.

⚠️ 프로덕션 무관: rambdaA/rambdaB는 이 파일을 import하지 않는다. 채점만 한다.

⚠️ 정직한 한계 (원장에도 같은 문구를 남긴다):
  유니버스 편입은 '그 시점의 거래대금 상위 100'으로 정해지는데, **과거 시점의
  거래대금 데이터가 없다**(아카이브된 universe 스냅샷은 이미 해외가 제외된 결과물이고,
  네이버 API는 현재값만 준다). 따라서 과거 구간 채점은 **오늘의 편입 목록을 과거에
  적용**하는 부트스트랩이며 생존편향이 있다. 진짜 OOS는 이 모듈이 도입된 시점부터다.
  다만 편입되는 해외 ETF 대부분(미국S&P500/나스닥100 등)은 수년째 거래대금 최상위라
  편향의 크기는 국내 종목보다 작을 것으로 본다 — 그래도 '작을 것'은 추측이므로
  판정은 전진 구간으로만 한다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))

import fdr                                                   # noqa: E402
from signal_generator import (                               # noqa: E402
    EXCLUDE_KEYWORDS, VOLUME_CUTOFF, LOOKBACK, classify_sector)

# 현행 운용이 "국내 ETF 전용"을 위해 막고 있는 키워드. 이걸 빼면 해외가 들어온다.
FOREIGN_BLOCK = ["미국", "나스닥", "S&P", "글로벌", "선진국", "MSCI",
                 "아시아", "신흥국", "이머징", "필라델피아", "차이나", "중국"]

# 해외 ETF를 어떤 섹터 버킷에 넣을지.
#   'single' → 전부 "해외" 한 버킷. 섹터당 1개 규칙 때문에 **최대 1종목(≈10%)**만 진입.
#   'split'  → 지역/테마별로 나눠 최대 3~4종목까지 진입 가능.
# 2026-08-28 실측: 해외 차단만 풀면 실제로 1종목(TIME 글로벌AI인공지능액티브)만
# 들어왔다 — 전부 classify_sector에서 "기타"로 뭉쳐 섹터 상한에 걸리기 때문이다.
# 즉 'single'은 사실상 현행과 큰 차이가 없고, 분산 효과를 보려면 'split'이 필요하다.
# 어느 쪽이 나은지는 정하지 않고 **둘 다 후보로 올려 원장이 답하게 한다.**
FOREIGN_SECTORS = [
    ("해외_미국지수",   ["S&P", "미국S&P", "미국500"]),
    ("해외_미국기술",   ["나스닥", "필라델피아", "미국테크", "미국AI"]),
    ("해외_선진",       ["선진국", "MSCI", "일본", "니케이", "유럽"]),
    ("해외_신흥",       ["신흥국", "이머징", "차이나", "중국", "인도", "베트남", "아시아"]),
    ("해외_글로벌테마", ["글로벌"]),
]


def _is_foreign(name: str) -> bool:
    return any(k in str(name) for k in FOREIGN_BLOCK)


def foreign_sector(name: str) -> str:
    """해외 ETF의 섹터 버킷. 어디에도 안 걸리면 포괄 버킷."""
    for bucket, kws in FOREIGN_SECTORS:
        if any(k in str(name) for k in kws):
            return bucket
    return "해외_기타"


class ForeignEtfDiscovery:
    """현행 필터에서 '해외 차단'만 뺐을 때 새로 편입되는 ETF 목록.

    프로덕션 `get_filtered_etf_list()`와 **같은 규칙**을 쓴다 — 같은 제외 키워드
    (해외 12종만 제외), 같은 거래대금 상위 VOLUME_CUTOFF 컷. 다른 건 그것뿐이다.
    """

    def __init__(self, cutoff: int = VOLUME_CUTOFF):
        self.cutoff = cutoff

    @staticmethod
    def _listing() -> pd.DataFrame:
        df = fdr.StockListing("ETF/KR")
        df.columns = [c.strip() for c in df.columns]
        # 프로덕션과 동일한 컬럼 탐지(fix15): 네이버 오타 필드 amonut이 거래대금이다.
        vol = ("amonut" if "amonut" in df.columns
               else "quant" if "quant" in df.columns else None)
        if vol is None:
            raise RuntimeError(f"거래대금 컬럼을 찾지 못함: {list(df.columns)}")
        df = df.rename(columns={vol: "Volume"})
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        return df

    def discover(self) -> pd.DataFrame:
        """해외 허용 시 유니버스에 새로 들어오는 종목만 반환 (Code, Name, Volume)."""
        df = self._listing()
        keep_all = [k for k in EXCLUDE_KEYWORDS if k not in FOREIGN_BLOCK]
        ext = df[df["Name"].apply(lambda n: not any(k in str(n) for k in keep_all))]
        ext = ext.nlargest(self.cutoff, "Volume")
        cur = df[df["Name"].apply(lambda n: not any(k in str(n) for k in EXCLUDE_KEYWORDS))]
        cur = cur.nlargest(self.cutoff, "Volume")
        newly = ext[~ext["Code"].isin(cur["Code"])]
        return newly[["Code", "Name", "Volume"]].reset_index(drop=True)


class ForeignAugmenter:
    """아카이브된 국내 유니버스에 해외 ETF를 **같은 모멘텀 정의로** 채점해 합친다.

    모멘텀은 signal_generator와 동일: base = (as_of − 126영업일) 이후 첫 거래일 종가,
    current = as_of 이하 마지막 종가, momentum = current/base − 1.
    """

    def __init__(self, names: dict, mode: str = "split"):
        """names: {code: name}. mode: 'single' | 'split'"""
        assert mode in ("single", "split")
        self.names = names
        self.mode = mode

    def _sector(self, name: str) -> str:
        if self.mode == "single":
            return "해외"
        s = foreign_sector(name)
        # 국내 섹터 키워드에도 걸리면(예: '글로벌반도체') 국내 쪽 분류를 존중해
        # 같은 테마가 두 버킷으로 갈라지는 것을 막는다(2026-07-20 사고 교훈).
        dom = classify_sector(name)
        return dom if dom != "기타" else s

    def score(self, as_of, prices) -> list[dict]:
        """as_of 시점 해외 ETF 채점 결과. 가격 없는 종목은 조용히가 아니라 '제외'."""
        base_date = as_of - timedelta(days=int(LOOKBACK * 7 / 5))
        out = []
        for code, name in self.names.items():
            s = prices.series(code)
            if s is None or s.empty:
                continue
            base_win = s[s.index <= pd.Timestamp(base_date + timedelta(days=30))].dropna()
            if base_win.empty:            # 신규 상장 방어(프로덕션과 동일)
                continue
            cur = s[s.index <= pd.Timestamp(as_of)].dropna()
            bas = s[s.index >= pd.Timestamp(base_date)].dropna()
            if cur.empty or bas.empty or float(bas.iloc[0]) == 0:
                continue
            out.append({
                "code": code, "name": name,
                "price": round(float(cur.iloc[-1]), 0),
                "momentum": round(float(cur.iloc[-1]) / float(bas.iloc[0]) - 1, 6),
                "sector": self._sector(name),
                "base_date": bas.index[0].strftime("%Y-%m-%d"),
                "base_price": round(float(bas.iloc[0]), 0),
                "foreign": True,
            })
        return out

    def augment(self, domestic: list[dict], as_of, prices) -> list[dict]:
        """국내(아카이브) + 해외(계산) 합본. 코드 중복은 국내 쪽을 남긴다."""
        have = {s["code"] for s in domestic}
        return list(domestic) + [s for s in self.score(as_of, prices)
                                 if s["code"] not in have]
