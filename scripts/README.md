# 주간 성과 분석 실행 매뉴얼 (Claude Code용 Runbook)

> 사용자가 "이번 주 실행 결과 분석해줘" / "주간 수익률 분석해줘"라고 하면 **이 문서의 STEP 0~6을
> 순서 그대로** 실행한다. 각 STEP의 명령은 수정 없이 복사해 실행한다. 판단이 필요한 부분은
> 문서에 규칙으로 명시되어 있으므로 임의로 변형하지 않는다.
>
> ⚠️ 이 작업 중에는 rambdaA/rambdaB 코드를 수정하지 않는다. Lambda를 실행하지 않는다.
> `data/` 폴더는 절대 커밋하지 않는다(.gitignore 처리됨).

---

## STEP 0. 사전 확인 (실패 시 아래 "문제 해결" 참조)

```bash
aws sts get-caller-identity --profile quantguard-ro
```
- 기대 결과: `"Account": "541371123603"` 이 포함된 JSON
- 실패하면 → 문제 해결 §A

## STEP 1. S3 아카이브 동기화

```bash
aws s3 sync s3://eunsung-quant-guard-bucket/latest_signal/ data/s3_archive/latest_signal/ --profile quantguard-ro
aws s3 sync s3://eunsung-quant-guard-bucket/quant_signals/ data/s3_archive/quant_signals/ --profile quantguard-ro
ls data/s3_archive/latest_signal/
```
- 기대 결과: `YYYY-MM-DD.json` 파일 목록. 최신 실행일 파일이 있어야 함.
- 사용자가 파일을 직접 줬더라도 sync를 실행한다 (S3가 원본).

## STEP 2. Notion에서 판단값 2개 읽기

Notion 데이터베이스: **주간 운영 일지** (data source: `collection://9da72eb2-0d91-4422-9106-d2bb415c323b`)

각 row에서 두 속성을 확인해 아래 변수를 만든다:

1. **제외 날짜 목록** ← `분석포함여부`(select)가 `제외` 인 row들의 `날짜`
   - 예: 2026-06-30은 잔고 폴백 버그로 항상 `제외`
   - 콤마로 연결: `"2026-06-30"` 또는 `"2026-06-30,2026-07-21"`
2. **순입출금 딕셔너리** ← `순입출금`(number)이 0이 아닌 row들의 `{날짜: 금액}`
   - 전부 0이거나 비어있으면 `'{}'`

- Notion 조회가 안 되면 → 문제 해결 §B

## STEP 3. 분석 스크립트 실행

```bash
dashboard/.venv/bin/python scripts/analyze_returns.py --exclude-dates "<STEP2의 제외날짜>" --net-deposits '<STEP2의 딕셔너리>'
```
실행 예시 (2026-07-14 기준 실제 사용례):
```bash
dashboard/.venv/bin/python scripts/analyze_returns.py --exclude-dates "2026-06-30" --net-deposits '{}'
```
- 기대 결과: "구간별 상대수익률" 표 + JSON 배열
- `force_test_mode=True` 기록(콘솔 테스트)은 스크립트가 **자동 제외**하므로 신경 쓰지 않는다
- "실전 실행 기록이 2개 미만" 메시지가 나오면 → 분석 불가를 사용자에게 알리고 종료
- 그 외 실패 → 문제 해결 §C

## STEP 4. Notion "주간 운영 일지"에 기록

스크립트 출력 JSON의 **각 구간(interval)마다**, `to` 날짜(YYYY-MM-DD)에 해당하는 row에 기록한다.

### 4-1. 해당 날짜 row가 이미 있으면 → 속성 3개만 업데이트
| Notion 속성 (정확한 이름) | JSON 키 |
|---|---|
| `주간수익률(%)` | `portfolio_return_pct` |
| `벤치마크수익률(%)` | `benchmark_return_pct` |
| `상대수익률(%p)` | `relative_return_pct` |

### 4-2. row가 없으면 → 새로 생성 (부모: 위 data source ID)
`data/s3_archive/latest_signal/<to날짜>.json` 을 읽어 아래처럼 채운다:

| Notion 속성 | 값 (출처) |
|---|---|
| `제목` (title) | `"YYYY-MM-DD (정기 리밸런싱)"` |
| `날짜` (date) | JSON `updated_at` 앞 10자리 |
| `시장상태` (select) | JSON `market_status` (BULL 또는 BEAR) |
| `총자산` / `운용자산` (number) | JSON `total_equity_checked` / `investable_asset` |
| `매도건수` / `매수건수` (number) | JSON `korea.sell_orders` / `korea.buy_orders` 배열 길이 |
| `순입출금` (number) | 0 |
| `분석포함여부` (select) | `포함` |
| `주간수익률(%)` 등 3종 | 4-1과 동일 |
| `특이사항` (text) | 특기할 이벤트 있으면 1~2문장 (없으면 생략) |

## STEP 5. HISTORY 확인 (조건부 — 해당 없으면 건너뜀)

직전 주에 "실계좌 검증 대기" 상태의 HISTORY 항목(버전 히스토리 DB:
`collection://adf356da-240a-4472-a749-57f639a71461`)이 있고, 이번 실행 결과가 그 검증을
완료시키는 경우에만 해당 항목의 테스트 결과를 갱신한다. 없으면 이 STEP은 건너뛴다.

## STEP 6. 사용자 보고 (이 형식을 따를 것)

1. **표**: 구간 | 포트폴리오 | KOSPI200 | 상대수익률 (STEP 3 출력 그대로)
2. **누적**: 첫 실행 총자산 → 최신 총자산의 수익률과 벤치마크 누적 비교
3. **해석** (아래 규칙을 그대로 적용):
   - 상대수익률 **+** = 시장보다 잘함 (계좌가 손실이어도 시장보다 덜 잃었으면 잘한 것)
   - 상대수익률 **−** = 시장보다 못함
   - 데이터 4~5주 미만이면 반드시 "추세 판단은 아직 이르다"를 명시
4. **특이 관찰**: BEAR 발동 여부, 매매 이상(매수/매도 0건 등), 시그널 검증(모멘텀 base) 이상 시 언급

---

## 문제 해결

### §A. AWS 인증 실패 (`aws` 없음 / credentials 오류)
- `which aws` 로 설치 확인. 없으면: 공식 pkg 설치 필요 — 사용자에게
  `sudo installer -pkg /tmp/AWSCLIV2.pkg -target /` 안내 (다운로드: `curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg`)
- 프로필 없음 오류면: 사용자가 직접 `aws configure --profile quantguard-ro` 실행해야 함
  (Access Key는 사용자만 보유. 리전 `ap-northeast-2`, output `json`)
- ⚠️ Homebrew의 awscli는 python 충돌 이력 있음 — 공식 pkg 사용

### §B. Notion 조회 불가
- 제외 날짜 기본값 `"2026-06-30"`, 순입출금 `'{}'` 으로 STEP 3을 진행하되,
  보고 시 "Notion 미연결로 기본값 사용, 입출금 있었으면 결과 보정 필요"를 명시
- STEP 4(기록)는 건너뛰고 결과만 보고

### §C. 스크립트 오류
- `ModuleNotFoundError` → 반드시 `dashboard/.venv/bin/python` 사용 (시스템 python3 아님)
- 벤치마크 조회 실패(야후) → 몇 분 후 1회 재시도, 계속 실패 시 포트폴리오 수익률만 보고
- 그 외 에러는 원문을 사용자에게 보여주고 중단 (임의 수정 금지)

## 배경 지식 (수정 시 참고)
- 벤치마크: KODEX 200 (069500), 조회는 `rambdaA/yf.py` 재사용 — 분석 시점에 야후에서 과거 시세를 받아오므로 분석이 며칠 늦어도 결과 동일
- 수익률 정의: `(기말총자산 − 순입출금) / 기초총자산 − 1`, 구간은 실전 실행(스냅샷) 사이
- 이 파이프라인 구축 이력: TODO "aws s3 sync 수집 자동화" (2026-07-10 완료), fix18 검증 분석 (2026-07-14)
