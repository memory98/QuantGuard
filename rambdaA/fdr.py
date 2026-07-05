# fdr.py — 네이버 API 기반 KRX ETF 목록 수집 커스텀 모듈
# 버전: v1.0.20260705.1
import urllib.request
import json
import pandas as pd

def StockListing(market):
    url = "https://finance.naver.com/api/sise/etfItemList.nhn"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            # 한글 인코딩 규격(cp949) 및 에러 무시 가드 적용
            raw_bytes = res.read()
            try:
                decoded_data = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                decoded_data = raw_bytes.decode('cp949', errors='ignore')
                
            data = json.loads(decoded_data)
    except Exception as e:
        raise Exception(f"네이버 ETF 수집망 통신 실패: {e}")
        
    df = pd.DataFrame(data['result']['etfItemList'])
    return df.rename(columns={'itemcode': 'Code', 'itemname': 'Name', 'amount': 'Volume'})