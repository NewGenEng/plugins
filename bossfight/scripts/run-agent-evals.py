#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


NEUTRAL_NAMES = (
    "amber-cove",
    "cedar-lake",
    "cobalt-hill",
    "fern-river",
    "lunar-field",
    "maple-brook",
    "opal-grove",
    "silver-pine",
)
CASE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class EvalError(Exception):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalError(f"expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def safe_relative(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{where} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise EvalError(f"{where} must be a relative path without parent traversal")
    return value


def digest_path(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    digest.update(b"directory\0")
    for entry in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = entry.relative_to(path).as_posix().encode("utf-8")
        if entry.is_symlink():
            digest.update(b"symlink\0" + relative)
        elif entry.is_dir():
            digest.update(b"dir\0" + relative)
        elif entry.is_file():
            digest.update(b"file\0" + relative + b"\0" + entry.read_bytes())
    return digest.hexdigest()


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def restore_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def parse_check(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvalError(f"{where} must be an object")
    unknown = set(raw) - {"name", "argv", "cwd", "timeout_seconds"}
    if unknown:
        raise EvalError(f"unknown {where} fields: {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EvalError(f"{where}.name must be a non-empty string")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
        raise EvalError(f"{where}.argv must be a non-empty string array")
    cwd = safe_relative(raw.get("cwd", "."), f"{where}.cwd")
    timeout = raw.get("timeout_seconds", 300)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise EvalError(f"{where}.timeout_seconds must be an integer from 1 to 3600")
    return {"name": name.strip(), "argv": argv, "cwd": cwd, "timeout_seconds": timeout}


def load_suite(path: Path) -> list[dict[str, Any]]:
    path = path.resolve()
    raw = read_json(path)
    unknown = set(raw) - {"schema_version", "cases"}
    if unknown:
        raise EvalError(f"unknown suite fields: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != 1:
        raise EvalError("suite schema_version must be 1")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalError("suite cases must be a non-empty array")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        where = f"cases[{index}]"
        if not isinstance(raw_case, dict):
            raise EvalError(f"{where} must be an object")
        unknown = set(raw_case) - {"id", "fixture", "prompt", "checks", "holdout", "protected"}
        if unknown:
            raise EvalError(f"unknown {where} fields: {', '.join(sorted(unknown))}")
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            raise EvalError(f"{where}.id must be lowercase kebab-case")
        if case_id in seen:
            raise EvalError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        fixture_value = safe_relative(raw_case.get("fixture"), f"{where}.fixture")
        fixture = (path.parent / fixture_value).resolve()
        if not fixture.is_dir():
            raise EvalError(f"fixture is not a directory: {fixture}")
        prompt = raw_case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise EvalError(f"{where}.prompt must be a non-empty string")
        raw_checks = raw_case.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            raise EvalError(f"{where}.checks must be a non-empty array")
        checks = [parse_check(check, f"{where}.checks[{check_index}]") for check_index, check in enumerate(raw_checks)]
        holdout_raw = raw_case.get("holdout", [])
        protected_raw = raw_case.get("protected", [])
        if not isinstance(holdout_raw, list) or not all(isinstance(value, str) for value in holdout_raw):
            raise EvalError(f"{where}.holdout must be a string array")
        if not isinstance(protected_raw, list) or not all(isinstance(value, str) for value in protected_raw):
            raise EvalError(f"{where}.protected must be a string array")
        holdout = [safe_relative(value, f"{where}.holdout") for value in holdout_raw]
        protected = [safe_relative(value, f"{where}.protected") for value in protected_raw]
        if len(set(holdout)) != len(holdout) or len(set(protected)) != len(protected):
            raise EvalError(f"{where}.holdout and protected paths must be unique")
        if set(holdout) & set(protected):
            raise EvalError(f"{where} cannot mark one path as both holdout and protected")
        for value in [*holdout, *protected]:
            if not (fixture / value).exists():
                raise EvalError(f"fixture path does not exist: {fixture / value}")
        cases.append(
            {
                "id": case_id,
                "fixture": fixture,
                "prompt": prompt.strip(),
                "checks": checks,
                "holdout": holdout,
                "protected": protected,
            }
        )
    return cases


def render_command(template: list[str], workspace: Path) -> list[str]:
    return [value.replace("{workspace}", str(workspace)) for value in template]


def clipped(value: str, limit: int = 16000) -> str:
    return value if len(value) <= limit else value[-limit:]


def run_check(check: dict[str, Any], workspace: Path) -> dict[str, Any]:
    cwd = (workspace / check["cwd"]).resolve()
    try:
        cwd.relative_to(workspace)
    except ValueError as exc:
        raise EvalError(f"check cwd escapes workspace: {cwd}") from exc
    if not cwd.is_dir():
        return {"name": check["name"], "passed": False, "exit_code": 127, "stdout": "", "stderr": f"missing cwd: {cwd}"}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            check["argv"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=check["timeout_seconds"],
            shell=False,
            check=False,
        )
        exit_code = completed.returncode
        stdout = clipped(completed.stdout)
        stderr = clipped(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = clipped((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        stderr = clipped((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        stderr = f"{stderr}\ncheck timed out after {check['timeout_seconds']} seconds".strip()
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
    return {
        "name": check["name"],
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
    }


def run_case(
    case: dict[str, Any],
    repeat_index: int,
    runner: list[str],
    prompt_prefix: str,
    timeout_seconds: int,
    output: Path,
    ordinal: int,
) -> dict[str, Any]:
    neutral = f"{NEUTRAL_NAMES[ordinal % len(NEUTRAL_NAMES)]}-{ordinal + 1:02d}"
    temporary = Path(tempfile.mkdtemp(prefix=f"{neutral}-"))
    workspace = temporary / "project"
    shutil.copytree(case["fixture"], workspace)
    protected_digests = {value: digest_path(workspace / value) for value in case["protected"]}
    for value in case["holdout"]:
        remove_path(workspace / value)
    prompt = f"{prompt_prefix.rstrip()}\n\n{case['prompt']}".strip() if prompt_prefix else case["prompt"]
    command = render_command(runner, workspace)
    started = time.monotonic()
    timed_out = False
    try:
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
            runner_exit = completed.returncode
            runner_stdout = completed.stdout
            runner_stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            runner_exit = 124
            runner_stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            runner_stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            runner_stderr = f"{runner_stderr}\nagent timed out after {timeout_seconds} seconds".strip()
        except OSError as exc:
            runner_exit = 127
            runner_stdout = ""
            runner_stderr = str(exc)
        protected_checks = []
        for value, expected in protected_digests.items():
            actual = digest_path(workspace / value)
            protected_checks.append(
                {
                    "name": f"protected:{value}",
                    "passed": actual == expected,
                    "exit_code": 0 if actual == expected else 1,
                    "stdout": "",
                    "stderr": "" if actual == expected else f"protected fixture changed: {value}",
                }
            )
        for value in case["holdout"]:
            restore_path(case["fixture"] / value, workspace / value)
        checks = [*protected_checks, *(run_check(check, workspace) for check in case["checks"])]
        run_output = output / "runs" / case["id"] / f"run-{repeat_index + 1:02d}"
        run_output.mkdir(parents=True, exist_ok=True)
        (run_output / "stdout.txt").write_text(runner_stdout, encoding="utf-8")
        (run_output / "stderr.txt").write_text(runner_stderr, encoding="utf-8")
        shutil.copytree(workspace, run_output / "workspace")
        result = {
            "case": case["id"],
            "run": repeat_index + 1,
            "workspace_label": neutral,
            "runner": {"argv": command, "exit_code": runner_exit, "timed_out": timed_out},
            "checks": checks,
            "passed": runner_exit == 0 and all(check["passed"] for check in checks),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "artifacts": str(run_output),
        }
        atomic_write_json(run_output / "result.json", result)
        return result
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def run_suite(
    suite_path: Path,
    runner: list[str],
    output: Path,
    jobs: int = 1,
    repeat: int = 1,
    prompt_prefix: str = "",
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    if not runner:
        raise EvalError("runner command cannot be empty")
    if jobs < 1 or repeat < 1:
        raise EvalError("jobs and repeat must be positive")
    cases = load_suite(suite_path)
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise EvalError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    tasks = [(case, run_index) for case in cases for run_index in range(repeat)]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(jobs, len(tasks))) as executor:
        futures = {
            executor.submit(run_case, case, run_index, runner, prompt_prefix, timeout_seconds, output, ordinal): (case["id"], run_index)
            for ordinal, (case, run_index) in enumerate(tasks)
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda result: (result["case"], result["run"]))
    passed = sum(1 for result in results if result["passed"])
    report = {
        "suite": str(suite_path.resolve()),
        "runs": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "success": passed == len(results),
        "results": results,
    }
    atomic_write_json(output / "report.json", report)
    return report


def parse_prompt_prefix(value: str) -> str:
    if value.startswith("@"):
        path = Path(value[1:]).resolve()
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise EvalError(f"prompt prefix file does not exist: {path}") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated agent tasks and grade them with executable checks.")
    parser.add_argument("suite", type=Path)
    parser.add_argument("--runner", required=True, help="quoted command; use {workspace} for the isolated project path")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--prompt-prefix", default="")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.timeout_seconds <= 21600:
            raise EvalError("timeout-seconds must be from 1 to 21600")
        runner = shlex.split(args.runner)
        report = run_suite(
            args.suite,
            runner,
            args.output,
            jobs=args.jobs,
            repeat=args.repeat,
            prompt_prefix=parse_prompt_prefix(args.prompt_prefix),
            timeout_seconds=args.timeout_seconds,
        )
    except EvalError as exc:
        print(f"agent-evals: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
