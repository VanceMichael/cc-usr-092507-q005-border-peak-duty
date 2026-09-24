import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from border_peak_duty.context import load_context

class ContextTest(unittest.TestCase):
    def test_fixture_has_expected_domain(self):
        value = load_context(Path("fixtures/domain.json"))
        self.assertEqual(value["domain"], "border-peak-duty")
        self.assertGreaterEqual(len(value["facts"]), 3)
        self.assertGreaterEqual(len(value["constraints"]), 3)

    def test_incomplete_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"version": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "缺少必要字段"):
                load_context(path)

if __name__ == "__main__":
    unittest.main()
