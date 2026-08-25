from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bossfight = load_module("bossfight_cli", PLUGIN_ROOT / "scripts" / "bossfight.py")
agent_evals = load_module("bossfight_agent_evals", PLUGIN_ROOT / "scripts" / "run-agent-evals.py")


class BossfightGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reference = self.root / "reference.txt"
        self.reference.write_text("reference artifact\n", encoding="utf-8")
        self.spec_path = self.root / "graph.json"
        self.run_dir = self.root / "run"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def spec(self, *, compare: bool = False, command: list[str] | None = None) -> dict:
        checks = []
        if command is not None:
            checks.append({"name": "acceptance", "argv": command, "cwd": ".", "timeout_seconds": 30})
        return {
            "schema_version": 1,
            "goal": "Build a directly verifiable artifact.",
            "bar": {
                "name": "Fixture reference",
                "source": "https://example.com/reference",
                "artifact": "reference.txt",
                "question": "Which artifact satisfies the stated behavior more clearly?",
            },
            "nodes": [
                {
                    "id": "build",
                    "title": "Build the artifact",
                    "depends_on": [],
                    "compare": compare,
                    "checks": checks,
                },
                {
                    "id": "final",
                    "kind": "final",
                    "title": "Judge the integrated artifact",
                    "depends_on": ["build"],
                    "compare": True,
                    "checks": [],
                },
            ],
        }

    def initialize(self, spec: dict) -> dict:
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return bossfight.init_run(self.spec_path, self.run_dir)

    def workspace(self, name: str = "workspace") -> tuple[Path, Path]:
        workspace = self.root / name
        workspace.mkdir()
        artifact = workspace / "artifact.txt"
        artifact.write_text(f"artifact from {name}\n", encoding="utf-8")
        return workspace, artifact

    def pass_comparison(self, node_id: str, seed: int = 1) -> dict:
        bossfight.prepare_judge(self.run_dir, node_id, seed=seed)
        attempt = bossfight.latest_attempt(self.run_dir, node_id)
        assert attempt is not None
        mapping = bossfight.comparison_mapping(self.run_dir, bossfight.load_graph(self.run_dir), attempt)
        ours_label = next(label for label, identity in mapping.items() if identity == "ours")
        result = self.root / f"{node_id}-critic.json"
        result.write_text(
            json.dumps(
                {
                    "winner": ours_label,
                    "biggest_gap": "The winner could still shorten one label.",
                    "evidence": ["The successful path is directly observable."],
                }
            ),
            encoding="utf-8",
        )
        return bossfight.record_verdict(self.run_dir, node_id, result)

    def test_rejects_cycles(self) -> None:
        spec = self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"])
        spec["nodes"][0]["depends_on"] = ["final"]
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(bossfight.BossfightError, "cycle"):
            bossfight.init_run(self.spec_path, self.run_dir)

    def test_final_must_cover_every_terminal_work_node(self) -> None:
        spec = self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"])
        spec["nodes"].insert(
            1,
            {
                "id": "docs",
                "title": "Write the guide",
                "depends_on": [],
                "compare": True,
                "checks": [],
            },
        )
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(bossfight.BossfightError, "terminal work nodes: docs"):
            bossfight.init_run(self.spec_path, self.run_dir)

    def test_init_is_idempotent_and_root_is_ready(self) -> None:
        spec = self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"])
        first = self.initialize(spec)
        second = bossfight.init_run(self.spec_path, self.run_dir)
        self.assertEqual(first, second)
        report = bossfight.status_report(self.run_dir)
        self.assertEqual(report["ready"], ["build"])
        self.assertFalse(report["complete"])

    def test_failed_check_becomes_focused_retry_feedback(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import pathlib,sys;sys.exit(0 if pathlib.Path('ok').exists() else 7)",
        ]
        self.initialize(self.spec(command=command))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "first try")
        check_result = bossfight.run_checks(self.run_dir, "build")
        self.assertFalse(check_result["passed"])
        self.assertEqual(bossfight.status_report(self.run_dir)["ready"], ["build"])
        second = bossfight.start_attempt(self.run_dir, "build", workspace)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["feedback"]["kind"], "failed-checks")
        self.assertEqual(second["feedback"]["failures"][0]["exit_code"], 7)

    def test_checks_only_win_unlocks_final(self) -> None:
        command = [sys.executable, "-c", "raise SystemExit(0)"]
        self.initialize(self.spec(command=command))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        self.assertTrue(bossfight.run_checks(self.run_dir, "build")["passed"])
        report = bossfight.status_report(self.run_dir)
        self.assertEqual(report["nodes"][0]["status"], "won")
        self.assertEqual(report["ready"], ["final"])

    def test_blind_win_maps_label_and_hides_identity(self) -> None:
        self.initialize(self.spec(compare=True))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        bossfight.run_checks(self.run_dir, "build")
        request = bossfight.prepare_judge(self.run_dir, "build", seed=9)
        rendered = json.dumps(request).lower()
        self.assertNotIn("ours", rendered)
        self.assertNotIn("reference", rendered)
        attempt = bossfight.latest_attempt(self.run_dir, "build")
        assert attempt is not None
        self.assertFalse((attempt / "comparison" / "key.json").exists())
        verdict = self.pass_comparison("build", seed=9)
        self.assertEqual(verdict["mapped_winner"], "ours")
        self.assertEqual(bossfight.status_report(self.run_dir)["ready"], ["final"])

    def test_blind_loss_feeds_one_gap_to_next_attempt(self) -> None:
        self.initialize(self.spec(compare=True))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        bossfight.run_checks(self.run_dir, "build")
        bossfight.prepare_judge(self.run_dir, "build", seed=3)
        attempt = bossfight.latest_attempt(self.run_dir, "build")
        assert attempt is not None
        mapping = bossfight.comparison_mapping(self.run_dir, bossfight.load_graph(self.run_dir), attempt)
        bar_label = next(label for label, identity in mapping.items() if identity == "bar")
        result = self.root / "critic.json"
        result.write_text(
            json.dumps(
                {
                    "winner": bar_label,
                    "biggest_gap": "Recovery output is missing.",
                    "evidence": ["Artifact B shows a correction while A exits silently."],
                }
            ),
            encoding="utf-8",
        )
        bossfight.record_verdict(self.run_dir, "build", result)
        retry = bossfight.start_attempt(self.run_dir, "build", workspace)
        self.assertEqual(retry["feedback"]["kind"], "critic-gap")
        self.assertEqual(retry["feedback"]["biggest_gap"], "Recovery output is missing.")

    def test_rejects_file_against_directory_comparison(self) -> None:
        self.initialize(self.spec(compare=True))
        workspace = self.root / "workspace"
        artifact = workspace / "artifact"
        artifact.mkdir(parents=True)
        (artifact / "result.txt").write_text("result\n", encoding="utf-8")
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        bossfight.run_checks(self.run_dir, "build")
        with self.assertRaisesRegex(bossfight.BossfightError, "not directly comparable"):
            bossfight.prepare_judge(self.run_dir, "build")

    def test_rejects_identical_artifacts_as_no_provable_win(self) -> None:
        self.initialize(self.spec(compare=True))
        workspace = self.root / "workspace"
        workspace.mkdir()
        artifact = workspace / "artifact.txt"
        artifact.write_text("reference artifact\n", encoding="utf-8")
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "identical")
        bossfight.run_checks(self.run_dir, "build")
        with self.assertRaisesRegex(bossfight.BossfightError, "identical"):
            bossfight.prepare_judge(self.run_dir, "build")

    def test_start_returns_existing_unfinished_attempt(self) -> None:
        self.initialize(self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"]))
        workspace, _ = self.workspace()
        first = bossfight.start_attempt(self.run_dir, "build", workspace)
        second = bossfight.start_attempt(self.run_dir, "build", workspace)
        self.assertEqual(first, second)
        self.assertEqual(len(bossfight.attempts(self.run_dir, "build")), 1)

    def test_rejects_artifact_outside_workspace(self) -> None:
        self.initialize(self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"]))
        workspace, _ = self.workspace()
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        bossfight.start_attempt(self.run_dir, "build", workspace)
        with self.assertRaisesRegex(bossfight.BossfightError, "inside the node workspace"):
            bossfight.record_build(self.run_dir, "build", outside, "bad")

    def test_rejects_shared_workspace_between_nodes(self) -> None:
        spec = self.spec(compare=True)
        spec["nodes"].insert(
            1,
            {"id": "docs", "title": "Write docs", "depends_on": [], "compare": True, "checks": []},
        )
        spec["nodes"][-1]["depends_on"] = ["build", "docs"]
        self.initialize(spec)
        workspace, _ = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        with self.assertRaisesRegex(bossfight.BossfightError, "owned by node build"):
            bossfight.start_attempt(self.run_dir, "docs", workspace)

    def test_doctor_detects_reference_and_build_drift(self) -> None:
        self.initialize(self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"]))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        attempt = bossfight.latest_attempt(self.run_dir, "build")
        assert attempt is not None
        (attempt / "build-artifact" / "artifact").write_text("changed\n", encoding="utf-8")
        captured_bar = self.run_dir / "bar" / "reference"
        captured_bar.write_text("changed bar\n", encoding="utf-8")
        report = bossfight.doctor_report(self.run_dir)
        self.assertFalse(report["ok"])
        rendered = "\n".join(report["issues"])
        self.assertIn("frozen bar changed", rendered)
        self.assertIn("build artifact snapshot changed", rendered)

    def test_checks_reject_source_edits_after_snapshot(self) -> None:
        self.initialize(self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"]))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "done")
        artifact.write_text("changed after record\n", encoding="utf-8")
        with self.assertRaisesRegex(bossfight.BossfightError, "changed before checks"):
            bossfight.run_checks(self.run_dir, "build")

    def test_retry_edits_do_not_corrupt_prior_attempt_evidence(self) -> None:
        command = [sys.executable, "-c", "raise SystemExit(7)"]
        self.initialize(self.spec(command=command))
        workspace, artifact = self.workspace()
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "first")
        bossfight.run_checks(self.run_dir, "build")
        first_attempt = bossfight.latest_attempt(self.run_dir, "build")
        assert first_attempt is not None
        artifact.write_text("second version\n", encoding="utf-8")
        bossfight.start_attempt(self.run_dir, "build", workspace)
        bossfight.record_build(self.run_dir, "build", artifact, "second")
        self.assertEqual((first_attempt / "build-artifact" / "artifact").read_text(), "artifact from workspace\n")
        self.assertTrue(bossfight.doctor_report(self.run_dir)["ok"])

    def test_whole_graph_can_reach_verified_completion(self) -> None:
        self.initialize(self.spec(compare=True))
        build_workspace, build_artifact = self.workspace("build-workspace")
        bossfight.start_attempt(self.run_dir, "build", build_workspace)
        bossfight.record_build(self.run_dir, "build", build_artifact, "component")
        bossfight.run_checks(self.run_dir, "build")
        self.pass_comparison("build", seed=2)
        final_workspace, final_artifact = self.workspace("final-workspace")
        bossfight.start_attempt(self.run_dir, "final", final_workspace)
        bossfight.record_build(self.run_dir, "final", final_artifact, "integrated")
        bossfight.run_checks(self.run_dir, "final")
        self.pass_comparison("final", seed=4)
        self.assertTrue(bossfight.status_report(self.run_dir)["complete"])
        self.assertEqual(bossfight.doctor_report(self.run_dir), {"ok": True, "complete": True, "issues": []})
        diagram = bossfight.mermaid_report(self.run_dir)
        self.assertIn("n_build --> n_final", diagram)
        self.assertIn("[won]", diagram)

    def test_cli_status_is_machine_readable(self) -> None:
        self.initialize(self.spec(command=[sys.executable, "-c", "raise SystemExit(0)"]))
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "bossfight.py"), "status", str(self.run_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["ready"], ["build"])


class AgentEvalRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixtures" / "orbit-notes"
        self.fixture.mkdir(parents=True)
        (self.fixture / "contract.json").write_text('{"value": "frozen"}\n', encoding="utf-8")
        (self.fixture / "hidden.txt").write_text("holdout\n", encoding="utf-8")
        (self.fixture / "acceptance.py").write_text(
            "from pathlib import Path\nraise SystemExit(0 if Path('done.txt').read_text() == 'done\\n' else 1)\n",
            encoding="utf-8",
        )
        self.suite = self.root / "suite.json"
        self.suite.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "orbit-notes",
                            "fixture": "fixtures/orbit-notes",
                            "prompt": "Add the missing deterministic export behavior.",
                            "protected": ["contract.json"],
                            "holdout": ["hidden.txt"],
                            "checks": [
                                {
                                    "name": "acceptance",
                                    "argv": [sys.executable, "acceptance.py"],
                                    "cwd": ".",
                                    "timeout_seconds": 30,
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_runner(self, writes_result: bool) -> Path:
        runner = self.root / ("passing-runner.py" if writes_result else "failing-runner.py")
        body = "from pathlib import Path\nimport sys\nsys.stdin.read()\n"
        if writes_result:
            body += "Path('done.txt').write_text('done\\n')\n"
        runner.write_text(body, encoding="utf-8")
        return runner

    def test_runs_repeated_cases_concurrently_and_preserves_artifacts(self) -> None:
        output = self.root / "output"
        report = agent_evals.run_suite(
            self.suite,
            [sys.executable, str(self.fake_runner(True))],
            output,
            jobs=2,
            repeat=2,
        )
        self.assertTrue(report["success"])
        self.assertEqual(report["passed"], 2)
        self.assertTrue((output / "report.json").is_file())
        for result in report["results"]:
            run_output = Path(result["artifacts"])
            self.assertEqual((run_output / "workspace" / "done.txt").read_text(), "done\n")

    def test_reports_executable_failure(self) -> None:
        output = self.root / "output"
        report = agent_evals.run_suite(
            self.suite,
            [sys.executable, str(self.fake_runner(False))],
            output,
        )
        self.assertFalse(report["success"])
        self.assertEqual(report["failed"], 1)
        acceptance = next(check for check in report["results"][0]["checks"] if check["name"] == "acceptance")
        self.assertEqual(acceptance["exit_code"], 1)

    def test_detects_protected_fixture_tampering(self) -> None:
        runner = self.root / "tampering-runner.py"
        runner.write_text(
            "from pathlib import Path\nimport sys\nsys.stdin.read()\nPath('contract.json').write_text('{}')\nPath('done.txt').write_text('done\\n')\n",
            encoding="utf-8",
        )
        report = agent_evals.run_suite(
            self.suite,
            [sys.executable, str(runner)],
            self.root / "output",
        )
        self.assertFalse(report["success"])
        protection = next(check for check in report["results"][0]["checks"] if check["name"].startswith("protected:"))
        self.assertFalse(protection["passed"])

    def test_refuses_to_mix_new_results_into_existing_output(self) -> None:
        output = self.root / "output"
        output.mkdir()
        (output / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(agent_evals.EvalError, "not empty"):
            agent_evals.run_suite(
                self.suite,
                [sys.executable, str(self.fake_runner(True))],
                output,
            )


if __name__ == "__main__":
    unittest.main()
