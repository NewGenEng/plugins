import json
from pathlib import Path

from slugger import slugify


cases = json.loads(Path("reference_cases.json").read_text(encoding="utf-8"))
failures = []
for case in cases:
    actual = slugify(case["input"])
    if actual != case["expected"]:
        failures.append({"input": case["input"], "expected": case["expected"], "actual": actual})
print(json.dumps({"passed": len(cases) - len(failures), "failed": failures}, indent=2))
