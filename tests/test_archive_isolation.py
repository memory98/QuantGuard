#!/usr/bin/env python3
"""
tests/test_archive_isolation.py — [fix42] rambdaB 테스트 아카이브 격리(#OPEN-ISO).

검증 강도 V2(변이 구별) + V3(구조 불변식).
fix42 이전 코드는 아래 `test_test_run_does_not_block_live_run`을 통과하지 못한다 —
테스트 실행이 실전 키에 기록했기 때문에 다음 정기 실행이 중복 가드에 걸렸다.

재현하는 사고 경로(2026-09-29 감사에서 발견, 실사고 전 선제 차단):
  월 10:00 콘솔 테스트(FORCE_TEST_MODE=True, force_run) → latest_signal/<월>.json 생성
  → 월 14:20 정기 실행이 head_object에 걸려 DUPLICATE_RUN_BLOCKED
  → 그 주가 BEAR면 DD가드 대피 매도 미실행(손실 방향, fail-safe 아님)
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import config  # noqa: E402
import lambda_function as lf  # noqa: E402
import archive_keys as ak  # noqa: E402  (rambdaA의 s3_keys와 모듈명 충돌 회피)

TODAY = "2026-09-28"


class KeyAwareS3:
    """키별로 존재 여부를 다르게 답하는 가짜 S3 (put 기록)."""

    def __init__(self, existing_keys=()):
        self.existing = set(existing_keys)
        self.puts = []

    def head_object(self, Bucket, Key):
        if Key in self.existing:
            return {}
        raise Exception("404 NoSuchKey")

    def get_object(self, Bucket, Key):
        raise Exception("404")            # prev_equity 없음 → weekly_return None

    def put_object(self, Bucket, Key, Body):
        self.puts.append(Key)
        self.existing.add(Key)   # 쓴 것은 이후 head_object에 '존재'로 보여야 한다
        return {}


class TestKeyMapping(unittest.TestCase):
    """V3 구조 불변식 — 실전 키가 config와 갈라지면 즉시 실패한다."""

    def test_live_latest_key_is_single_source(self):
        latest, _ = ak.archive_keys(TODAY, is_test=False)
        self.assertEqual(latest, config.SIGNAL_FILE_KEY,
                         "실전 최신본 키가 config.SIGNAL_FILE_KEY와 갈라졌다")

    def test_live_keys(self):
        self.assertEqual(ak.archive_keys(TODAY, is_test=False),
                         ("latest_signal.json", f"latest_signal/{TODAY}.json"))

    def test_test_keys_are_separated(self):
        latest, archive = ak.archive_keys(TODAY, is_test=True)
        self.assertEqual(latest, "latest_signal_test.json")
        self.assertEqual(archive, f"latest_signal_test/{TODAY}.json")
        self.assertNotIn("latest_signal/", archive)

    def test_duplicate_guard_always_checks_live(self):
        self.assertEqual(ak.live_archive_key(TODAY), f"latest_signal/{TODAY}.json")


class TestHandlerIsolation(unittest.TestCase):

    def _run(self, event, fake_s3, force_test_mode, korea_result=None):
        korea_result = korea_result or {"result": "BULL_REBALANCING_SUCCESS",
                                        "market_status": "BULL"}
        fixed_now = __import__("datetime").datetime(2026, 9, 28, 5, 20, 15)  # UTC → KST 14:20
        with mock.patch.object(lf, "boto3") as m_boto, \
             mock.patch.object(lf, "KIS_APPKEY", "k"), \
             mock.patch.object(lf, "KIS_APPSECRET", "s"), \
             mock.patch.object(lf, "KIS_ACCOUNT", "a"), \
             mock.patch.object(lf, "FORCE_TEST_MODE", force_test_mode), \
             mock.patch.object(lf, "get_access_token", return_value="tok"), \
             mock.patch.object(lf, "fetch_total_equity", return_value=2_700_000), \
             mock.patch.object(lf, "run_korea_rebalancing", return_value=korea_result) as m_korea, \
             mock.patch.object(lf, "run_usa_rebalancing", return_value={"result": "USA_OK"}), \
             mock.patch.object(lf, "send_telegram"), \
             mock.patch.object(lf, "CASH_RESERVE", 0):
            m_boto.client.return_value = fake_s3
            with mock.patch.object(lf.datetime, "datetime") as m_dt:
                m_dt.utcnow.return_value = fixed_now
                m_dt.side_effect = lambda *a, **k: __import__("datetime").datetime(*a, **k)
                res = lf.lambda_handler(event, None)
        return res, m_korea

    def test_test_run_writes_only_isolated_keys(self):
        fake = KeyAwareS3()
        self._run({"force_run": True}, fake, force_test_mode=True)
        self.assertTrue(fake.puts, "테스트 실행도 기록은 남겨야 한다")
        for key in fake.puts:
            self.assertTrue(key.startswith("latest_signal_test"),
                            f"테스트 실행이 실전 키에 기록했다: {key}")

    def test_live_run_writes_live_keys(self):
        fake = KeyAwareS3()
        self._run({"force_run": True}, fake, force_test_mode=False)
        self.assertIn(f"latest_signal/{TODAY}.json", fake.puts)
        self.assertIn("latest_signal.json", fake.puts)

    def test_test_run_does_not_block_live_run(self):
        """사고 재현(핵심) — 같은 날 ① 콘솔 테스트 → ② 정기 실행 순서로 실제로 이어서 돌린다.

        ①이 무엇을 남기든 ②는 돌아야 한다. fix42 이전에는 ①이 latest_signal/<날짜>.json을
        써서 ②가 DUPLICATE_RUN_BLOCKED로 죽었다(그 주 리밸런싱 전체 취소).
        """
        fake = KeyAwareS3()

        # ① 월 10:00 — 사용자가 스키마 검증용 콘솔 테스트 실행 (CLAUDE.md가 지시하는 절차)
        self._run({"force_run": True}, fake, force_test_mode=True)
        self.assertTrue(fake.puts, "테스트 실행도 기록은 남긴다")

        # ② 월 14:20 — 정기 스케줄 실행 (force_run 없음)
        res, m_korea = self._run({}, fake, force_test_mode=False)
        self.assertNotEqual(res.get("body"), "DUPLICATE_RUN_BLOCKED",
                            "오전 테스트 때문에 그 주 실전 리밸런싱이 차단됐다(#OPEN-ISO 재발)")
        m_korea.assert_called_once()
        self.assertIn(f"latest_signal/{TODAY}.json", fake.puts,
                      "정기 실행은 실전 아카이브를 남겨야 한다")

    def test_live_run_then_duplicate_is_still_blocked(self):
        """반대방향 — 실전이 먼저 돌았으면 두 번째 실행은 여전히 차단(2026-06-30 보호 유지)."""
        fake = KeyAwareS3()
        self._run({"force_run": True}, fake, force_test_mode=False)   # ① 실전 실행
        res, m_korea = self._run({}, fake, force_test_mode=False)     # ② 중복 실행 시도
        self.assertEqual(res["body"], "DUPLICATE_RUN_BLOCKED")
        m_korea.assert_not_called()

    def test_live_archive_still_blocks_duplicate(self):
        """회귀 반대방향 — 진짜 실전 기록이 있으면 여전히 차단해야 한다(2026-06-30 보호 유지)."""
        fake = KeyAwareS3(existing_keys={f"latest_signal/{TODAY}.json"})
        res, m_korea = self._run({}, fake, force_test_mode=False)
        self.assertEqual(res["body"], "DUPLICATE_RUN_BLOCKED")
        m_korea.assert_not_called()
        self.assertEqual(fake.puts, [])


if __name__ == "__main__":
    unittest.main()
