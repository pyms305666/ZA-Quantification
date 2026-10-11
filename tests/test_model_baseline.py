"""Immutable synthetic inputs lock evaluation outputs and input request order."""
import json
from pathlib import Path
import unittest

from config import RiskConfig
from market.decision_profiles import PROFILES, requests_for
from market.evaluator import evaluate
from tests.test_decision_profiles import INSTRUMENT, NOW, history, fresh_quote


class ModelBaselineTests(unittest.TestCase):
    def test_all_horizons_match_original_inputs_and_outputs(self):
        expected = json.loads((Path(__file__).parent / "fixtures/model-v1.2.1.json").read_text(encoding="utf-8"))
        actual = {m: dict(requests=requests_for(m),
                         result=evaluate(INSTRUMENT, fresh_quote(), history(), RiskConfig(), m, NOW))
                  for m in PROFILES}
        # JSON canonicalization accounts for tuple/list serialization only.
        self.assertEqual(json.loads(json.dumps(actual)), expected)
