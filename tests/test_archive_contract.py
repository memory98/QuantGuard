#!/usr/bin/env python3
"""tests/test_archive_contract.py — 생산자(korea) ↔ 아카이브(lambda_function) 계약 [L6ⓔ]

2026-08-31 실사고:
  fix33이 `run_korea_rebalancing`의 반환 dict에 `execution_audit`을 추가했는데,
  `lambda_function`의 BULL 경로가 `korea`를 **화이트리스트로 다시 조립**하고 있어
  그 키가 조용히 버려졌다. 에러도 경고도 없어서 2주간(08-24·08-31) 아무도 몰랐고,
  스위치를 켰더라도 아카이브엔 아무것도 안 남았을 것이다.
  게다가 BEAR 경로는 `korea_result`를 통째로 넣어 **경로별로 동작이 달랐다.**

이 테스트가 막는 것:
  생산자가 새 키를 만들었는데 아무도 분류하지 않은 상태를 CI에서 실패시킨다.
  "아카이브에 넣는다" 또는 "일부러 뺀다" 둘 중 하나를 **명시적으로 선언**하게 강제한다.
  키를 늘릴 때 이 파일이 같이 안 고쳐지면 배포가 막힌다.

왜 화이트리스트를 없애지 않나:
  `name_map`·`targets`는 크고(종목명 사전, 후보 15종 전체) S3 아카이브를 부풀린다.
  통째로 넣는 편이 단순하지만 저장 비용과 가독성 때문에 선별이 의도된 설계다.
  문제는 선별 자체가 아니라 **선별이 조용했다는 것**이라, 침묵만 없앤다.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaB"))

# ── 분류표: 생산자의 모든 키는 아래 둘 중 하나에 반드시 속해야 한다 ──────────
ARCHIVED = {
    "sell_orders", "buy_orders", "executed_orders", "reinvest_orders",
    "skipped_band", "buys_skipped_unsettled", "sell_settled",
    "execution_audit",          # [fix38] 2026-08-31 추가 — 이게 빠져 있던 게 사고
}
DELIBERATELY_EXCLUDED = {
    "result":        "핸들러가 분기용으로만 쓴다. 아카이브의 market_status와 중복.",
    "market_status": "output_signal 최상위에 이미 있다.",
    "sell_count":    "sell_orders 길이로 계산 가능(중복 저장 안 함).",
    "buy_count":     "buy_orders 길이로 계산 가능(중복 저장 안 함).",
    "name_map":      "종목명 사전 — 텔레그램 영수증 전용. 아카이브를 크게 부풀린다.",
    "targets":       "후보 15종 전체 — quant_signals 아카이브에 이미 있다.",
    "failed_sells":  "BEAR 경로 전용. BEAR는 korea_result를 통째로 저장한다.",
}


def _returned_keys(func_name: str) -> set:
    """korea.py를 AST로 읽어 지정 함수의 `return {...}` 리터럴 키를 모은다.

    함수를 실제로 호출하면 KIS API를 타야 하므로 정적 분석으로 계약만 본다.
    """
    tree = ast.parse((ROOT / "rambdaB" / "korea.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == func_name)
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _archive_whitelist() -> set:
    """lambda_function.py의 BULL 경로 `"korea": { ... }` 리터럴 키를 읽는다."""
    tree = ast.parse((ROOT / "rambdaB" / "lambda_function.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "korea"
                    and isinstance(v, ast.Dict)):
                found.append({kk.value for kk in v.keys
                              if isinstance(kk, ast.Constant)})
    assert found, "lambda_function에서 `\"korea\": {...}` 리터럴을 찾지 못했다"
    return max(found, key=len)


class TestArchiveContract(unittest.TestCase):

    def setUp(self):
        self.produced = _returned_keys("run_korea_rebalancing")
        self.whitelist = _archive_whitelist()

    def test_every_produced_key_is_classified(self):
        """생산자가 만든 키 중 '아카이브' 도 '의도적 제외' 도 아닌 것이 있으면 실패.

        새 필드를 추가하고 이 파일을 안 고치면 여기서 걸린다 — 그게 목적이다.
        """
        classified = ARCHIVED | set(DELIBERATELY_EXCLUDED)
        unclassified = self.produced - classified
        self.assertEqual(
            unclassified, set(),
            msg=("korea.py가 새 키를 반환하는데 분류되지 않았다: "
                 f"{sorted(unclassified)}\n"
                 "→ 아카이브에 남길 것이면 lambda_function의 화이트리스트와 "
                 "이 파일의 ARCHIVED 에 함께 추가하고, 뺄 것이면 "
                 "DELIBERATELY_EXCLUDED 에 이유와 함께 적어라."))

    def test_whitelist_matches_archived_set(self):
        """실제 화이트리스트가 ARCHIVED와 정확히 일치하는가(양방향)."""
        self.assertEqual(self.whitelist, ARCHIVED)

    def test_execution_audit_is_archived(self):
        """2026-08-31 사고의 회귀 방지 — 이 키가 다시 빠지면 실패."""
        self.assertIn("execution_audit", self.whitelist)
        self.assertIn("execution_audit", self.produced)

    def test_archived_keys_actually_exist_in_producer(self):
        """반대 방향: 아카이브가 생산자에 없는 키를 기대하고 있지 않은가.

        생산자에서 키를 지웠는데 화이트리스트에 남아 있으면 매번 빈 기본값이
        저장돼, 소비자(analyze_returns 등)는 '값이 없는 것'과 '0'을 구분 못 한다.
        """
        stale = ARCHIVED - self.produced
        self.assertEqual(stale, set(),
                         msg=f"생산자에 없는 키를 아카이브가 기대한다: {sorted(stale)}")

    def test_bear_path_keys_also_classified(self):
        """BEAR 경로는 korea_result를 통째로 저장한다 — 그 키들도 분류돼 있는가.

        경로별로 저장 필드가 다르면 소비자가 날짜마다 다른 스키마를 만난다.
        """
        bear = _returned_keys("run_korea_rebalancing")
        classified = ARCHIVED | set(DELIBERATELY_EXCLUDED)
        self.assertEqual(bear - classified, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
