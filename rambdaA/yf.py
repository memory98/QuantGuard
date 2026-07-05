# yf.py — 커스텀 야후 파이낸스 데이터 수집 모듈
# 버전: v1.0.20260705.1
# [수정 이력]
#   fix9:  range=1y → range=2y 로 변경
#          None 값 그대로 유지 (날짜 인덱스 보존)
#   fix10: close → adjclose(수정 주가) 로 교체
#          - ETF 분배금 지급 시 원시 close 는 분배락 당일 급락 반영
#          - adjclose 는 분배금을 소급 보정한 연속성 있는 가격
#          - 모멘텀 계산 오염(140~300%+) 근본 원인 해결
#          - adjclose 없으면 close 로 fallback (VIX 등 지수 대응)

import urllib.request
import json
import pandas as pd
from datetime import datetime

def download(tickers, start=None, end=None, **kwargs):
    if isinstance(tickers, str):
        tickers = [tickers]

    all_data = {}

    for ticker in tickers:
        symbol = ticker.replace(".KS", "")

        if symbol == "^VIX":
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d"
        else:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.KS?range=2y&interval=1d"

        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = json.loads(res.read().decode('utf-8'))

            chart      = raw['chart']['result'][0]
            timestamps = chart['timestamp']

            # [fix10] adjclose(수정 주가) 우선 사용, 없으면 close fallback
            # adjclose: 분배금·액면분할 소급 반영 → 모멘텀 계산 정확도 확보
            try:
                closes = chart['indicators']['adjclose'][0]['adjclose']
            except (KeyError, IndexError):
                closes = chart['indicators']['quote'][0]['close']

            dates = [datetime.fromtimestamp(ts).strftime('%Y-%m-%d') for ts in timestamps]
            df    = pd.DataFrame({'Close': closes}, index=pd.to_datetime(dates))

            all_data[ticker] = df['Close']

        except Exception:
            continue

    if not all_data:
        return pd.DataFrame()

    res_df = pd.DataFrame(all_data)

    if len(tickers) == 1:
        res_df.columns = ['Close']
    else:
        res_df.columns = tickers

    return res_df