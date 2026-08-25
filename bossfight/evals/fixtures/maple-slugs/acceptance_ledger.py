import json
from pathlib import Path


run = Path(".bossfight/maple-slugs")
graph = json.loads((run / "graph.json").read_text(encoding="utf-8"))
assert graph["schema_version"] == 1
assert graph["bar"]["fingerprint"].startswith("sha256:")
finals = [node for node in graph["nodes"] if node.get("kind") == "final"]
assert len(finals) == 1
final = finals[0]
attempts = sorted((run / "nodes" / final["id"] / "attempts").iterdir())
assert attempts
latest = attempts[-1]
verdict = json.loads((latest / "verdict.json").read_text(encoding="utf-8"))
assert verdict["mapped_winner"] == "ours"
request = json.loads((latest / "comparison" / "judge-request.json").read_text(encoding="utf-8"))
rendered = json.dumps(request).lower()
assert "ours" not in rendered
assert set(request["artifacts"]) == {"A", "B"}
comparison = latest / "comparison"
artifacts = [comparison / label / "artifact" for label in ("A", "B")]
assert all(artifact.is_file() for artifact in artifacts) or all(artifact.is_dir() for artifact in artifacts)
for artifact in artifacts:
    implementation = artifact if artifact.is_file() else artifact / "slugger.py"
    assert implementation.is_file()
    assert "def slugify" in implementation.read_text(encoding="utf-8")
