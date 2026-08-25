import json
import unittest
from pathlib import Path

from slugger import slugify


class SluggerAcceptanceTests(unittest.TestCase):
    def test_reference_cases(self) -> None:
        cases = json.loads(Path("reference_cases.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(value=case["input"]):
                self.assertEqual(slugify(case["input"]), case["expected"])

    def test_public_api_rejects_non_strings(self) -> None:
        with self.assertRaises(TypeError):
            slugify(None)


if __name__ == "__main__":
    unittest.main()
