#!/usr/bin/env python3
"""
tests/test_handler_guards.py — [감사 fix29] 오케스트레이터 안전장치 e2e (lambda_function).

lambda_handler는 💰💰💰 실돈 진입점인데 테스트가 0개(V0)였다. 실사고에서 나온 3대
안전장치를 e2e로 핀 박는다(회귀 방지):
  ① 중복 실행 가드 (2026-06-30 사고): 오늘자 아카이브가 있고 force_run 없으면
     DUPLICATE_RUN_BLOCKED — 매매 함수를 절대 호출하지 않음.
  ② CASH_RESERVE 초과 가드: 예치금이 총자산보다 크면 매매 없이 안전 종료.
  ③ BEAR 분기 상태: BEAR 대피 결과면 market_status=BEAR, 미국 스킵, 아카이브 기록.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import lambda_function as lf  # noqa: E402


class FakeS3:
    """head/get/put를 제어 가능한 가짜 S3. put 호출을 기록."""
    def __init__(self, today_archive_exists=False, prev_data=None):
        self.today_archive_exists = today_archive_exists
        self.prev_data = prev_data
        self.puts = []

    def head_object(self, Bucket, Key):
        if self.today_archive_exists:
            return {}                     # 존재 → 중복
        raise Exception("404 NoSuchKey")  # 없음 → 첫 실행

    def get_object(self, Bucket, Key):
        if self.prev_data is None:
            raise Exception("404")
        body = mock.Mock()
        body.read.return_value = json.dumps(self.prev_data).encode("utf-8")
        return {"Body": body}

    def put_object(self, Bucket, Key, Body):
        self.puts.append(Key)
        return {}


class TestHandlerGuards(unittest.TestCase):
    def _run(self, event, fake_s3, equity=2_700_000, korea_result=None,
             cash_reserve=0):
        korea_result = korea_result or {"result": "BULL_REBALANCING_SUCCESS",
                                        "market_status": "BULL"}
        with mock.patch.object(lf, "boto3") as m_boto, \
             mock.patch.object(lf, "KIS_APPKEY", "k"), \
             mock.patch.object(lf, "KIS_APPSECRET", "s"), \
             mock.patch.object(lf, "KIS_ACCOUNT", "a"), \
             mock.patch.object(lf, "get_access_token", return_value="tok"), \
             mock.patch.object(lf, "fetch_total_equity", return_value=equity), \
             mock.patch.object(lf, "run_korea_rebalancing",
                               return_value=korea_result) as m_korea, \
             mock.patch.object(lf, "run_usa_rebalancing",
                               return_value={"result": "USA_OK"}) as m_usa, \
             mock.patch.object(lf, "send_telegram") as m_tele, \
             mock.patch.object(lf, "CASH_RESERVE", cash_reserve):
            m_boto.client.return_value = fake_s3
            res = lf.lambda_handler(event, None)
        self._last_tele = m_tele
        return res, m_korea, m_usa

    def test_duplicate_run_blocked(self):
        """① 오늘자 아카이브 존재 + force_run 없음 → 차단, 매매 함수 미호출."""
        fake = FakeS3(today_archive_exists=True)
        res, m_korea, m_usa = self._run({}, fake)
        self.assertEqual(res["body"], "DUPLICATE_RUN_BLOCKED")
        m_korea.assert_not_called()
        m_usa.assert_not_called()
        self.assertEqual(fake.puts, [], "차단 시 S3 기록이 없어야 함")

    def test_force_run_bypasses_duplicate_guard(self):
        """force_run=true면 아카이브가 있어도 진행(매매 함수 호출)."""
        fake = FakeS3(today_archive_exists=True)
        res, m_korea, _ = self._run({"force_run": True}, fake)
        m_korea.assert_called_once()

    def test_cash_reserve_exceeds_equity_aborts(self):
        """② CASH_RESERVE > 총자산 → 안전 종료, 매매 미호출."""
        fake = FakeS3(today_archive_exists=False)
        res, m_korea, _ = self._run({"force_run": True}, fake,
                                    equity=1_000_000, cash_reserve=5_000_000)
        body = json.loads(res["body"])
        self.assertEqual(body["result"], "CASH_RESERVE_EXCEEDED")
        m_korea.assert_not_called()

    def test_bear_branch_skips_usa_and_archives(self):
        """③ BEAR 대피 결과 → market_status BEAR, 미국 스킵, 아카이브 기록."""
        fake = FakeS3(today_archive_exists=False)
        bear = {"result": "BEAR_SHELTER_EXECUTED", "market_status": "BEAR",
                "sell_orders": [], "buy_orders": []}
        res, m_korea, m_usa = self._run({"force_run": True}, fake,
                                        korea_result=bear)
        body = json.loads(res["body"])
        self.assertEqual(body["market_status"], "BEAR")
        m_usa.assert_not_called()               # 미국 ETF 스킵
        self.assertTrue(len(fake.puts) >= 1, "BEAR에도 아카이브를 남겨야 함")

    def test_silent_skips_now_alert(self):
        """④ [fix30] STALE/S3_ERROR/NO_TARGETS는 텔레그램 경고 — 조용한 미실행 방지."""
        for result in ("STALE_SIGNAL_ABORT", "S3_SIGNAL_ERROR", "NO_TARGETS"):
            with self.subTest(result=result):
                fake = FakeS3(today_archive_exists=False)
                self._run({"force_run": True}, fake,
                          korea_result={"result": result})
                self.assertTrue(self._last_tele.called,
                                f"{result}은 텔레그램 경고를 보내야 함")

    def test_market_closed_stays_silent(self):
        """휴장일(MARKET_CLOSED)은 정상이라 경고 스팸을 내지 않는다."""
        fake = FakeS3(today_archive_exists=False)
        self._run({"force_run": True}, fake,
                  korea_result={"result": "MARKET_CLOSED"})
        self.assertFalse(self._last_tele.called,
                         "휴장일은 매주 알림을 보내면 안 됨")


if __name__ == "__main__":
    unittest.main(verbosity=2)
