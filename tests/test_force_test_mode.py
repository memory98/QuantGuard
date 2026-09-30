#!/usr/bin/env python3
"""
tests/test_force_test_mode.py — [fix44] FORCE_TEST_MODE 환경변수화 + 오설정 방어.

이 스위치 하나가 **실주문 여부**를 가른다. 이전엔 config.py 하드코딩이라 콘솔 테스트마다
True로 고쳐 배포 → 테스트 → False로 되돌려 재배포가 필요했고, 되돌리기를 놓치면
그 주 정기 실행이 Mock으로 돌아 리밸런싱이 조용히 사라졌다(BEAR 주면 대피 매도 미실행).

검증 강도 V2(변이 구별) + V4(값 스윕): 환경변수 파싱을 표로 훑고, 오설정 2경로에서
매매 함수가 **호출되지 않는지**를 e2e로 확인한다.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))
import lambda_function as lf  # noqa: E402


def reload_config(value):
    """FORCE_TEST_MODE 환경변수를 주고 rambdaB/config.py를 **경로로** 새로 읽는다.

    `import config`를 쓰면 안 된다 — rambdaA에도 config.py가 있어 전체 스위트에서는
    먼저 로드된 쪽이 sys.modules를 점유한다(원장 #OPEN-C). 실제로 이 테스트가 단독으로는
    통과하고 전체 실행에서만 깨져서 그 충돌을 재현했다. 경로 기반 로드는 매번 독립
    모듈이라 전역 오염도 없다(reload 방식의 부작용 제거).
    """
    env = dict(os.environ)
    env.pop("FORCE_TEST_MODE", None)
    if value is not None:
        env["FORCE_TEST_MODE"] = value
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location(
            "_rambdab_config_probe", ROOT / "rambdaB" / "config.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class TestEnvParsing(unittest.TestCase):
    """V4 값 스윕 — 실주문을 가르는 파싱이라 경계를 전수 확인한다."""

    def test_unset_defaults_to_live(self):
        """미설정이면 실전 — 콘솔 설정 없이도 기존 배포본과 동일하게 동작해야 한다."""
        c = reload_config(None)
        self.assertFalse(c.FORCE_TEST_MODE)
        self.assertFalse(c.FORCE_TEST_MODE_INVALID)

    def test_truthy_values(self):
        for v in ("true", "True", "TRUE", " true ", "1", "yes", "on"):
            c = reload_config(v)
            self.assertTrue(c.FORCE_TEST_MODE, f"{v!r} → 테스트 모드여야 함")
            self.assertFalse(c.FORCE_TEST_MODE_INVALID, f"{v!r}는 유효값")

    def test_falsy_values(self):
        for v in ("false", "False", "0", "no", "off", " FALSE "):
            c = reload_config(v)
            self.assertFalse(c.FORCE_TEST_MODE, f"{v!r} → 실전이어야 함")
            self.assertFalse(c.FORCE_TEST_MODE_INVALID, f"{v!r}는 유효값")

    def test_garbage_falls_to_no_order_side_and_flags_invalid(self):
        """오타는 '실전 주문'으로 흘러선 안 된다 — 주문 없는 쪽 + invalid 표시."""
        for v in ("ture", "", "maybe", "2", "y"):
            c = reload_config(v)
            self.assertTrue(c.FORCE_TEST_MODE, f"{v!r}에서 실주문 쪽으로 떨어졌다")
            self.assertTrue(c.FORCE_TEST_MODE_INVALID, f"{v!r}가 invalid로 표시되지 않았다")


class FakeS3:
    def __init__(self):
        self.puts = []

    def head_object(self, Bucket, Key):
        raise Exception("404")

    def get_object(self, Bucket, Key):
        raise Exception("404")

    def put_object(self, Bucket, Key, Body):
        self.puts.append(Key)
        return {}


class TestMisconfigGuards(unittest.TestCase):
    """오설정이면 매매 함수를 아예 부르지 않아야 한다."""

    def _run(self, event, *, test_mode, invalid):
        fake = FakeS3()
        with mock.patch.object(lf, "boto3") as m_boto, \
             mock.patch.object(lf, "KIS_APPKEY", "k"), \
             mock.patch.object(lf, "KIS_APPSECRET", "s"), \
             mock.patch.object(lf, "KIS_ACCOUNT", "a"), \
             mock.patch.object(lf, "FORCE_TEST_MODE", test_mode), \
             mock.patch.object(lf, "FORCE_TEST_MODE_INVALID", invalid), \
             mock.patch.object(lf, "get_access_token", return_value="tok"), \
             mock.patch.object(lf, "fetch_total_equity", return_value=2_700_000), \
             mock.patch.object(lf, "run_korea_rebalancing",
                               return_value={"result": "BULL_REBALANCING_SUCCESS",
                                             "market_status": "BULL"}) as m_korea, \
             mock.patch.object(lf, "run_usa_rebalancing", return_value={"result": "USA_OK"}), \
             mock.patch.object(lf, "send_telegram") as m_tele, \
             mock.patch.object(lf, "CASH_RESERVE", 0):
            m_boto.client.return_value = fake
            res = lf.lambda_handler(event, None)
        return res, m_korea, m_tele, fake

    def test_invalid_value_aborts_without_trading(self):
        res, m_korea, m_tele, fake = self._run({"force_run": True},
                                               test_mode=True, invalid=True)
        self.assertEqual(json.loads(res["body"])["result"], "FORCE_TEST_MODE_INVALID")
        m_korea.assert_not_called()
        self.assertEqual(fake.puts, [], "중단인데 S3 기록이 남았다")
        m_tele.assert_called_once()

    def test_scheduled_run_in_test_mode_aborts_and_alerts(self):
        """되돌리기 누락 시나리오 — Mock으로 완주하면 그 주 리밸런싱 누락이 묻힌다."""
        res, m_korea, m_tele, _ = self._run({}, test_mode=True, invalid=False)
        self.assertEqual(json.loads(res["body"])["result"], "TEST_MODE_ON_SCHEDULED_RUN")
        m_korea.assert_not_called()
        m_tele.assert_called_once()
        self.assertIn("테스트 모드", m_tele.call_args[0][0])

    def test_manual_console_test_still_runs(self):
        """의도된 콘솔 테스트(force_run)는 그대로 돌아야 한다."""
        res, m_korea, _, fake = self._run({"force_run": True},
                                          test_mode=True, invalid=False)
        m_korea.assert_called_once()
        self.assertTrue(any(k.startswith("latest_signal_test") for k in fake.puts),
                        f"테스트 실행이 격리 키에 기록되지 않았다: {fake.puts}")

    def test_live_scheduled_run_unaffected(self):
        """실전 정기 실행은 아무 영향 없어야 한다(회귀 반대방향)."""
        res, m_korea, _, fake = self._run({}, test_mode=False, invalid=False)
        m_korea.assert_called_once()
        self.assertIn("latest_signal.json", fake.puts)


if __name__ == "__main__":
    unittest.main()
