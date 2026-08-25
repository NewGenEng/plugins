#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
NODE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ATTEMPT_ID = re.compile(r"^\d{4}$")


class BossfightError(Exception):
    pass


class Status(str, Enum):
    PENDING = "pending"
    READY = "ready"
    BUILDING = "building"
    CHECKING = "checking"
    JUDGING = "judging"
    RETRY = "retry"
    WON = "won"
    BLOCKED = "blocked"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BossfightError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BossfightError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BossfightError(f"expected a JSON object in {path}")
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def write_once(path: Path, value: Any) -> None:
    if path.exists():
        if read_json(path) != value:
            raise BossfightError(f"refusing to replace immutable fact: {path}")
        return
    atomic_write_json(path, value)


def ensure_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BossfightError(f"unknown {where} fields: {', '.join(unknown)}")


def require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BossfightError(f"{where} must be a non-empty string")
    return value.strip()


def within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_relative_path(value: Any, where: str) -> str:
    text = require_text(value, where)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise BossfightError(f"{where} must stay inside its workspace")
    return text


def digest_path(path: Path) -> str:
    path = path.resolve()
    if not path.exists():
        raise BossfightError(f"artifact does not exist: {path}")
    digest = hashlib.sha256()
    if path.is_symlink():
        raise BossfightError(f"artifact cannot be a symlink: {path}")
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return f"sha256:{digest.hexdigest()}"
    if not path.is_dir():
        raise BossfightError(f"artifact must be a file or directory: {path}")
    digest.update(b"directory\0")
    entries = sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix())
    for entry in entries:
        relative = entry.relative_to(path).as_posix().encode("utf-8")
        if entry.is_symlink():
            raise BossfightError(f"artifact tree cannot contain symlinks: {entry}")
        if entry.is_dir():
            digest.update(b"d\0" + relative + b"\0")
        elif entry.is_file():
            digest.update(b"f\0" + relative + b"\0")
            with entry.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def copy_artifact(source: Path, destination: Path) -> None:
    source = source.resolve()
    if source.is_symlink():
        raise BossfightError(f"artifact cannot be a symlink: {source}")
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return
    if source.is_dir():
        shutil.copytree(source, destination)
        return
    raise BossfightError(f"artifact must be a file or directory: {source}")


def parse_check(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BossfightError(f"{where} must be an object")
    ensure_keys(raw, {"name", "argv", "cwd", "timeout_seconds"}, where)
    name = require_text(raw.get("name"), f"{where}.name")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise BossfightError(f"{where}.argv must be a non-empty string array")
    cwd = safe_relative_path(raw.get("cwd", "."), f"{where}.cwd")
    timeout = raw.get("timeout_seconds", 300)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise BossfightError(f"{where}.timeout_seconds must be an integer from 1 to 3600")
    return {"name": name, "argv": argv, "cwd": cwd, "timeout_seconds": timeout}


def parse_graph(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    ensure_keys(raw, {"schema_version", "goal", "bar", "nodes"}, "graph")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise BossfightError(f"schema_version must be {SCHEMA_VERSION}")
    goal = require_text(raw.get("goal"), "goal")
    raw_bar = raw.get("bar")
    if not isinstance(raw_bar, dict):
        raise BossfightError("bar must be an object")
    ensure_keys(raw_bar, {"name", "source", "artifact", "question", "fingerprint"}, "bar")
    bar = {
        "name": require_text(raw_bar.get("name"), "bar.name"),
        "source": require_text(raw_bar.get("source"), "bar.source"),
        "artifact": safe_relative_path(raw_bar.get("artifact"), "bar.artifact"),
        "question": require_text(raw_bar.get("question"), "bar.question"),
    }
    if "fingerprint" in raw_bar:
        fingerprint = require_text(raw_bar["fingerprint"], "bar.fingerprint")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise BossfightError("bar.fingerprint must be a sha256 digest")
        bar["fingerprint"] = fingerprint
    bar_path = (base / bar["artifact"]).resolve()
    if not within(bar_path, base.resolve()) and "fingerprint" in bar:
        raise BossfightError("captured bar must stay inside the run directory")
    if not bar_path.exists():
        raise BossfightError(f"bar artifact does not exist: {bar_path}")

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise BossfightError("nodes must be a non-empty array")
    nodes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        where = f"nodes[{index}]"
        if not isinstance(raw_node, dict):
            raise BossfightError(f"{where} must be an object")
        ensure_keys(raw_node, {"id", "title", "kind", "depends_on", "compare", "checks"}, where)
        node_id = require_text(raw_node.get("id"), f"{where}.id")
        if not NODE_ID.fullmatch(node_id):
            raise BossfightError(f"{where}.id must be lowercase kebab-case")
        if node_id in ids:
            raise BossfightError(f"duplicate node id: {node_id}")
        ids.add(node_id)
        kind = raw_node.get("kind", "work")
        if kind not in {"work", "final"}:
            raise BossfightError(f"{where}.kind must be work or final")
        dependencies = raw_node.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise BossfightError(f"{where}.depends_on must be a string array")
        if len(set(dependencies)) != len(dependencies):
            raise BossfightError(f"{where}.depends_on contains duplicates")
        compare = raw_node.get("compare", True)
        if not isinstance(compare, bool):
            raise BossfightError(f"{where}.compare must be a boolean")
        raw_checks = raw_node.get("checks", [])
        if not isinstance(raw_checks, list):
            raise BossfightError(f"{where}.checks must be an array")
        checks = [parse_check(check, f"{where}.checks[{check_index}]") for check_index, check in enumerate(raw_checks)]
        if not compare and not checks:
            raise BossfightError(f"{node_id} needs a comparison or at least one deterministic check")
        nodes.append(
            {
                "id": node_id,
                "title": require_text(raw_node.get("title"), f"{where}.title"),
                "kind": kind,
                "depends_on": dependencies,
                "compare": compare,
                "checks": checks,
            }
        )

    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        for dependency in node["depends_on"]:
            if dependency not in by_id:
                raise BossfightError(f"{node['id']} depends on unknown node {dependency}")
            if dependency == node["id"]:
                raise BossfightError(f"{node['id']} cannot depend on itself")
    order = topological_order(nodes)
    finals = [node for node in nodes if node["kind"] == "final"]
    if len(finals) != 1:
        raise BossfightError("graph must contain exactly one final node")
    final = finals[0]
    if any(final["id"] in node["depends_on"] for node in nodes):
        raise BossfightError("the final node must be a sink")
    work_ids = {node["id"] for node in nodes if node["kind"] == "work"}
    nonterminal_work = {dependency for node in nodes if node["kind"] == "work" for dependency in node["depends_on"]}
    terminal_work = work_ids - nonterminal_work
    missing_terminal = sorted(terminal_work - set(final["depends_on"]))
    if missing_terminal:
        raise BossfightError(f"final node must depend on terminal work nodes: {', '.join(missing_terminal)}")
    if not final["compare"]:
        raise BossfightError("final node must use a blind comparison")
    node_rank = {node_id: index for index, node_id in enumerate(order)}
    nodes.sort(key=lambda node: node_rank[node["id"]])
    return {"schema_version": SCHEMA_VERSION, "goal": goal, "bar": bar, "nodes": nodes}


def topological_order(nodes: list[dict[str, Any]]) -> list[str]:
    dependencies = {node["id"]: set(node["depends_on"]) for node in nodes}
    ready = sorted(node_id for node_id, deps in dependencies.items() if not deps)
    order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for candidate in sorted(dependencies):
            if node_id in dependencies[candidate]:
                dependencies[candidate].remove(node_id)
                if not dependencies[candidate] and candidate not in order and candidate not in ready:
                    ready.append(candidate)
                    ready.sort()
    if len(order) != len(nodes):
        cycle_nodes = sorted(set(dependencies) - set(order))
        raise BossfightError(f"graph contains a cycle involving: {', '.join(cycle_nodes)}")
    return order


def source_fingerprint(graph: dict[str, Any], bar_digest: str) -> str:
    value = {"graph": graph, "source_bar_digest": bar_digest}
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def init_run(spec_path: Path, run_dir: Path) -> dict[str, Any]:
    spec_path = spec_path.resolve()
    source_graph = parse_graph(read_json(spec_path), spec_path.parent)
    source_bar = (spec_path.parent / source_graph["bar"]["artifact"]).resolve()
    bar_digest = digest_path(source_bar)
    fingerprint = source_fingerprint(source_graph, bar_digest)
    run_dir = run_dir.resolve()
    meta_path = run_dir / "run-meta.json"
    if meta_path.exists():
        meta = read_json(meta_path)
        if meta.get("source_fingerprint") != fingerprint:
            raise BossfightError("run already exists with a different graph or source bar")
        graph = load_graph(run_dir)
        verify_bar(run_dir, graph)
        return graph
    if run_dir.exists():
        raise BossfightError(f"run directory exists without a valid ledger: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=run_dir.parent))
    try:
        captured_bar = staging / "bar" / "reference"
        copy_artifact(source_bar, captured_bar)
        graph = json.loads(json.dumps(source_graph))
        graph["bar"]["artifact"] = "bar/reference"
        graph["bar"]["fingerprint"] = digest_path(captured_bar)
        for node in graph["nodes"]:
            (staging / "nodes" / node["id"] / "attempts").mkdir(parents=True)
        atomic_write_json(staging / "graph.json", graph)
        atomic_write_json(
            staging / "run-meta.json",
            {
                "schema_version": SCHEMA_VERSION,
                "source_fingerprint": fingerprint,
                "created_at_unix": int(time.time()),
            },
        )
        os.replace(staging, run_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return graph


def load_graph(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    return parse_graph(read_json(run_dir / "graph.json"), run_dir)


def verify_bar(run_dir: Path, graph: dict[str, Any]) -> None:
    expected = graph["bar"].get("fingerprint")
    if not expected:
        raise BossfightError("captured graph has no frozen bar fingerprint")
    actual = digest_path(run_dir / graph["bar"]["artifact"])
    if actual != expected:
        raise BossfightError(f"frozen bar changed: expected {expected}, got {actual}")


def node_by_id(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph["nodes"]:
        if node["id"] == node_id:
            return node
    raise BossfightError(f"unknown node: {node_id}")


def attempt_root(run_dir: Path, node_id: str) -> Path:
    return run_dir / "nodes" / node_id / "attempts"


def attempts(run_dir: Path, node_id: str) -> list[Path]:
    root = attempt_root(run_dir, node_id)
    if not root.is_dir():
        raise BossfightError(f"missing attempt directory for {node_id}")
    values = sorted(path for path in root.iterdir() if path.is_dir() and ATTEMPT_ID.fullmatch(path.name))
    return values


def latest_attempt(run_dir: Path, node_id: str) -> Path | None:
    values = attempts(run_dir, node_id)
    return values[-1] if values else None


def checks_passed(attempt: Path) -> bool:
    path = attempt / "checks.json"
    return path.exists() and read_json(path).get("passed") is True


def attempt_status(node: dict[str, Any], attempt: Path | None) -> Status:
    if attempt is None:
        return Status.READY
    intent = attempt / "intent.json"
    build = attempt / "build.json"
    checks = attempt / "checks.json"
    verdict = attempt / "verdict.json"
    if not intent.exists():
        return Status.BLOCKED
    if verdict.exists():
        winner = read_json(verdict).get("mapped_winner")
        if winner == "ours":
            return Status.WON
        if winner in {"bar", "tie"}:
            return Status.RETRY
        return Status.BLOCKED
    if checks.exists():
        if read_json(checks).get("passed") is not True:
            return Status.RETRY
        return Status.JUDGING if node["compare"] else Status.WON
    if build.exists():
        return Status.CHECKING
    return Status.BUILDING


def graph_states(run_dir: Path, graph: dict[str, Any]) -> dict[str, Status]:
    states: dict[str, Status] = {}
    for node in graph["nodes"]:
        dependencies_won = all(states.get(dependency) == Status.WON for dependency in node["depends_on"])
        current_attempt = latest_attempt(run_dir, node["id"])
        if not dependencies_won:
            states[node["id"]] = Status.BLOCKED if current_attempt else Status.PENDING
            continue
        states[node["id"]] = attempt_status(node, current_attempt)
    return states


def feedback_from(attempt: Path | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    checks_path = attempt / "checks.json"
    if checks_path.exists():
        checks = read_json(checks_path)
        if checks.get("passed") is not True:
            failures = [
                {
                    "name": result.get("name"),
                    "exit_code": result.get("exit_code"),
                    "stderr": result.get("stderr", ""),
                    "stdout": result.get("stdout", ""),
                }
                for result in checks.get("results", [])
                if result.get("passed") is not True
            ]
            return {"kind": "failed-checks", "failures": failures}
    verdict_path = attempt / "verdict.json"
    if verdict_path.exists():
        verdict = read_json(verdict_path)
        if verdict.get("mapped_winner") != "ours":
            return {
                "kind": "critic-gap",
                "winner": verdict.get("mapped_winner"),
                "biggest_gap": verdict.get("biggest_gap"),
                "evidence": verdict.get("evidence", []),
            }
    return None


def workspace_in_use(run_dir: Path, graph: dict[str, Any], workspace: Path, node_id: str) -> str | None:
    for node in graph["nodes"]:
        if node["id"] == node_id:
            continue
        for attempt in attempts(run_dir, node["id"]):
            intent_path = attempt / "intent.json"
            if intent_path.exists() and Path(read_json(intent_path)["workspace"]).resolve() == workspace:
                return node["id"]
    return None


def start_attempt(run_dir: Path, node_id: str, workspace: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise BossfightError(f"builder workspace is not a directory: {workspace}")
    graph = load_graph(run_dir)
    node = node_by_id(graph, node_id)
    states = graph_states(run_dir, graph)
    latest = latest_attempt(run_dir, node_id)
    if states[node_id] == Status.BUILDING and latest:
        return read_json(latest / "intent.json")
    if states[node_id] not in {Status.READY, Status.RETRY}:
        raise BossfightError(f"cannot start {node_id} while it is {states[node_id].value}")
    owner = workspace_in_use(run_dir, graph, workspace, node_id)
    if owner:
        raise BossfightError(f"workspace is already owned by node {owner}: {workspace}")
    previous = latest
    attempt_number = len(attempts(run_dir, node_id)) + 1
    attempt_name = f"{attempt_number:04d}"
    target = attempt_root(run_dir, node_id) / attempt_name
    staging = Path(tempfile.mkdtemp(prefix=f".{attempt_name}.", dir=target.parent))
    intent = {
        "schema_version": SCHEMA_VERSION,
        "node_id": node_id,
        "attempt": attempt_number,
        "workspace": str(workspace),
        "goal": graph["goal"],
        "title": node["title"],
        "feedback": feedback_from(previous),
    }
    try:
        atomic_write_json(staging / "intent.json", intent)
        os.replace(staging, target)
    except FileExistsError:
        if target.exists():
            return read_json(target / "intent.json")
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return intent


def current_attempt_for(run_dir: Path, graph: dict[str, Any], node_id: str) -> tuple[dict[str, Any], Path]:
    node = node_by_id(graph, node_id)
    attempt = latest_attempt(run_dir, node_id)
    if attempt is None:
        raise BossfightError(f"node has no attempt: {node_id}")
    return node, attempt


def record_build(run_dir: Path, node_id: str, artifact: Path, summary: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    graph = load_graph(run_dir)
    node, attempt = current_attempt_for(run_dir, graph, node_id)
    state = attempt_status(node, attempt)
    artifact = artifact.resolve()
    intent = read_json(attempt / "intent.json")
    workspace = Path(intent["workspace"]).resolve()
    if not within(artifact, workspace):
        raise BossfightError(f"artifact must live inside the node workspace: {workspace}")
    build_path = attempt / "build.json"
    if build_path.exists():
        existing = read_json(build_path)
        verify_build_artifact(attempt)
        if existing.get("artifact_source") != str(artifact) or existing.get("summary") != summary.strip():
            raise BossfightError(f"refusing to replace immutable build fact: {build_path}")
        return existing
    if state != Status.BUILDING:
        raise BossfightError(f"cannot record a build while {node_id} is {state.value}")
    source_digest = digest_path(artifact)
    snapshot_root = attempt / "build-artifact"
    snapshot = snapshot_root / "artifact"
    if snapshot_root.exists():
        if digest_path(snapshot) != source_digest:
            raise BossfightError(f"partial build snapshot does not match source artifact: {snapshot_root}")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".build-artifact.", dir=attempt))
        try:
            copy_artifact(artifact, staging / "artifact")
            os.replace(staging, snapshot_root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    payload = {
        "artifact": "build-artifact/artifact",
        "artifact_source": str(artifact),
        "artifact_relative": artifact.relative_to(workspace).as_posix(),
        "artifact_fingerprint": source_digest,
        "summary": summary.strip(),
    }
    write_once(build_path, payload)
    return payload


def verify_build_artifact(attempt: Path) -> dict[str, Any]:
    build = read_json(attempt / "build.json")
    relative = safe_relative_path(build.get("artifact"), "build.artifact")
    artifact = (attempt / relative).resolve()
    if not within(artifact, attempt.resolve()):
        raise BossfightError("build artifact snapshot escapes its attempt")
    expected = require_text(build.get("artifact_fingerprint"), "build.artifact_fingerprint")
    actual = digest_path(artifact)
    if actual != expected:
        raise BossfightError(f"build artifact snapshot changed: expected {expected}, got {actual}")
    return build


def verify_source_before_checks(attempt: Path, build: dict[str, Any]) -> None:
    source = Path(require_text(build.get("artifact_source"), "build.artifact_source")).resolve()
    expected = require_text(build.get("artifact_fingerprint"), "build.artifact_fingerprint")
    actual = digest_path(source)
    if actual != expected:
        raise BossfightError(f"builder artifact changed before checks: expected {expected}, got {actual}")


def truncate_output(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def run_checks(run_dir: Path, node_id: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    graph = load_graph(run_dir)
    node, attempt = current_attempt_for(run_dir, graph, node_id)
    checks_path = attempt / "checks.json"
    if checks_path.exists():
        verify_build_artifact(attempt)
        return read_json(checks_path)
    state = attempt_status(node, attempt)
    if state != Status.CHECKING:
        raise BossfightError(f"cannot run checks while {node_id} is {state.value}")
    build = verify_build_artifact(attempt)
    verify_source_before_checks(attempt, build)
    workspace = Path(read_json(attempt / "intent.json")["workspace"]).resolve()
    results: list[dict[str, Any]] = []
    for check in node["checks"]:
        cwd = (workspace / check["cwd"]).resolve()
        if not within(cwd, workspace) or not cwd.is_dir():
            raise BossfightError(f"check cwd is not a directory inside the workspace: {cwd}")
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
            stdout = truncate_output(completed.stdout)
            stderr = truncate_output(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = truncate_output((exc.stdout or "") if isinstance(exc.stdout, str) else "")
            stderr = truncate_output((exc.stderr or "") if isinstance(exc.stderr, str) else "")
            stderr = f"{stderr}\ncheck timed out after {check['timeout_seconds']} seconds".strip()
        except OSError as exc:
            exit_code = 127
            stdout = ""
            stderr = str(exc)
        results.append(
            {
                "name": check["name"],
                "argv": check["argv"],
                "cwd": check["cwd"],
                "exit_code": exit_code,
                "passed": exit_code == 0,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "stdout": stdout,
                "stderr": stderr,
            }
        )
    payload = {"passed": all(result["passed"] for result in results), "results": results}
    write_once(checks_path, payload)
    return payload


def materialize_label(source: Path, label_dir: Path) -> None:
    label_dir.mkdir(parents=True)
    copy_artifact(source, label_dir / "artifact")


def comparison_shape(path: Path) -> tuple[str, str]:
    path = path.resolve()
    if path.is_file():
        return ("file", path.suffix.lower())
    if path.is_dir():
        return ("directory", "")
    raise BossfightError(f"comparison artifact must be a file or directory: {path}")


def prepare_judge(run_dir: Path, node_id: str, seed: int | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    graph = load_graph(run_dir)
    verify_bar(run_dir, graph)
    node, attempt = current_attempt_for(run_dir, graph, node_id)
    comparison = attempt / "comparison"
    request_path = comparison / "judge-request.json"
    if request_path.exists():
        verify_build_artifact(attempt)
        return read_json(request_path)
    state = attempt_status(node, attempt)
    if state != Status.JUDGING or not node["compare"] or not checks_passed(attempt):
        raise BossfightError(f"cannot prepare a judge while {node_id} is {state.value}")
    build = verify_build_artifact(attempt)
    ours = (attempt / build["artifact"]).resolve()
    bar = run_dir / graph["bar"]["artifact"]
    ours_shape = comparison_shape(ours)
    bar_shape = comparison_shape(bar)
    if ours_shape != bar_shape:
        ours_description = f"{ours_shape[0]}{f' ({ours_shape[1]})' if ours_shape[1] else ''}"
        bar_description = f"{bar_shape[0]}{f' ({bar_shape[1]})' if bar_shape[1] else ''}"
        raise BossfightError(
            f"artifacts are not directly comparable: ours is {ours_description} and bar is {bar_description}"
        )
    if build["artifact_fingerprint"] == graph["bar"]["fingerprint"]:
        raise BossfightError("artifacts are identical; a blind critic cannot establish a win")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    ours_label = rng.choice(["A", "B"])
    mapping = {ours_label: "ours", "B" if ours_label == "A" else "A": "bar"}
    staging = Path(tempfile.mkdtemp(prefix=".comparison.", dir=attempt))
    try:
        for label in ("A", "B"):
            source = ours if mapping[label] == "ours" else bar
            materialize_label(source, staging / label)
        request = {
            "question": graph["bar"]["question"],
            "artifacts": {"A": "A/artifact", "B": "B/artifact"},
            "instructions": "Inspect both artifacts directly. Return only the verdict object. Do not score or praise.",
            "verdict_schema": {
                "winner": "A | B | tie | invalid",
                "biggest_gap": "one concrete remaining gap",
                "evidence": ["one or more directly observed facts"],
            },
        }
        atomic_write_json(staging / "judge-request.json", request)
        os.replace(staging, comparison)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return request


def parse_critic_result(path: Path) -> dict[str, Any]:
    raw = read_json(path.resolve())
    ensure_keys(raw, {"winner", "biggest_gap", "evidence"}, "critic result")
    winner = raw.get("winner")
    if winner not in {"A", "B", "tie", "invalid"}:
        raise BossfightError("critic winner must be A, B, tie, or invalid")
    gap = require_text(raw.get("biggest_gap"), "critic biggest_gap")
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
        raise BossfightError("critic evidence must be a non-empty string array")
    return {"winner": winner, "biggest_gap": gap, "evidence": [item.strip() for item in evidence]}


def comparison_mapping(run_dir: Path, graph: dict[str, Any], attempt: Path) -> dict[str, str]:
    expected = {
        "ours": require_text(read_json(attempt / "build.json").get("artifact_fingerprint"), "build.artifact_fingerprint"),
        "bar": require_text(graph["bar"].get("fingerprint"), "bar.fingerprint"),
    }
    if expected["ours"] == expected["bar"]:
        raise BossfightError("artifacts are identical; blind labels cannot be mapped uniquely")
    mapping: dict[str, str] = {}
    for label in ("A", "B"):
        actual = digest_path(attempt / "comparison" / label / "artifact")
        matches = [identity for identity, fingerprint in expected.items() if actual == fingerprint]
        if len(matches) != 1:
            raise BossfightError(f"comparison label {label} does not match one frozen artifact")
        mapping[label] = matches[0]
    if set(mapping.values()) != {"ours", "bar"}:
        raise BossfightError("blind labels do not contain exactly one build and one bar artifact")
    return mapping


def record_verdict(run_dir: Path, node_id: str, result_path: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    graph = load_graph(run_dir)
    node, attempt = current_attempt_for(run_dir, graph, node_id)
    verdict_path = attempt / "verdict.json"
    critic = parse_critic_result(result_path)
    labels = comparison_mapping(run_dir, graph, attempt)
    mapped = labels[critic["winner"]] if critic["winner"] in {"A", "B"} else critic["winner"]
    payload = {**critic, "mapped_winner": mapped}
    if verdict_path.exists():
        write_once(verdict_path, payload)
        return payload
    state = attempt_status(node, attempt)
    if state != Status.JUDGING or not (attempt / "comparison" / "judge-request.json").exists():
        raise BossfightError(f"cannot record a verdict while {node_id} is {state.value}")
    verify_build_artifact(attempt)
    write_once(verdict_path, payload)
    return payload


def status_report(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    graph = load_graph(run_dir)
    bar_intact = True
    bar_error = None
    try:
        verify_bar(run_dir, graph)
    except BossfightError as exc:
        bar_intact = False
        bar_error = str(exc)
    states = graph_states(run_dir, graph)
    nodes = []
    for node in graph["nodes"]:
        latest = latest_attempt(run_dir, node["id"])
        nodes.append(
            {
                "id": node["id"],
                "title": node["title"],
                "kind": node["kind"],
                "depends_on": node["depends_on"],
                "status": states[node["id"]].value,
                "attempts": len(attempts(run_dir, node["id"])),
                "feedback": feedback_from(latest),
            }
        )
    final = next(node for node in graph["nodes"] if node["kind"] == "final")
    return {
        "goal": graph["goal"],
        "bar": {"name": graph["bar"]["name"], "source": graph["bar"]["source"], "intact": bar_intact, "error": bar_error},
        "complete": bar_intact and states[final["id"]] == Status.WON,
        "ready": [node_id for node_id, state in states.items() if state in {Status.READY, Status.RETRY}],
        "nodes": nodes,
    }


def doctor_report(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    issues: list[str] = []
    try:
        graph = load_graph(run_dir)
    except BossfightError as exc:
        return {"ok": False, "issues": [str(exc)]}
    try:
        verify_bar(run_dir, graph)
    except BossfightError as exc:
        issues.append(str(exc))
    workspace_owners: dict[Path, str] = {}
    for node in graph["nodes"]:
        node_attempts = attempts(run_dir, node["id"])
        expected_names = [f"{index:04d}" for index in range(1, len(node_attempts) + 1)]
        actual_names = [attempt.name for attempt in node_attempts]
        if actual_names != expected_names:
            issues.append(f"{node['id']} has non-contiguous attempts: {actual_names}")
        for attempt in node_attempts:
            intent_path = attempt / "intent.json"
            if not intent_path.exists():
                issues.append(f"{node['id']}/{attempt.name} is missing intent.json")
                continue
            try:
                intent = read_json(intent_path)
                workspace = Path(intent["workspace"]).resolve()
                owner = workspace_owners.get(workspace)
                if owner and owner != node["id"]:
                    issues.append(f"workspace shared by {owner} and {node['id']}: {workspace}")
                workspace_owners[workspace] = node["id"]
            except (BossfightError, KeyError, TypeError) as exc:
                issues.append(f"invalid intent for {node['id']}/{attempt.name}: {exc}")
            build_path = attempt / "build.json"
            checks_path = attempt / "checks.json"
            comparison_path = attempt / "comparison"
            verdict_path = attempt / "verdict.json"
            if checks_path.exists() and not build_path.exists():
                issues.append(f"{node['id']}/{attempt.name} has checks without a build")
            if comparison_path.exists() and (not checks_path.exists() or not checks_passed(attempt)):
                issues.append(f"{node['id']}/{attempt.name} has a comparison without passing checks")
            if comparison_path.exists() and not node["compare"]:
                issues.append(f"{node['id']}/{attempt.name} compares a checks-only node")
            if verdict_path.exists() and not (comparison_path / "judge-request.json").exists():
                issues.append(f"{node['id']}/{attempt.name} has a verdict without a judge packet")
            if build_path.exists():
                try:
                    verify_build_artifact(attempt)
                except BossfightError as exc:
                    issues.append(f"{node['id']}/{attempt.name}: {exc}")
            if verdict_path.exists():
                try:
                    verdict = read_json(verdict_path)
                    mapping = comparison_mapping(run_dir, graph, attempt)
                    expected = mapping[verdict["winner"]] if verdict["winner"] in {"A", "B"} else verdict["winner"]
                    if verdict.get("mapped_winner") != expected:
                        issues.append(f"{node['id']}/{attempt.name} has an inconsistent verdict mapping")
                except (BossfightError, KeyError, TypeError) as exc:
                    issues.append(f"invalid verdict for {node['id']}/{attempt.name}: {exc}")
    report = status_report(run_dir)
    if report["complete"] and issues:
        issues.append("run cannot be complete while doctor issues remain")
    return {"ok": not issues, "complete": report["complete"] and not issues, "issues": issues}


def mermaid_report(run_dir: Path) -> str:
    graph = load_graph(run_dir.resolve())
    states = graph_states(run_dir.resolve(), graph)
    lines = ["flowchart LR"]
    for node in graph["nodes"]:
        identifier = f"n_{node['id'].replace('-', '_')}"
        label = f"{node['title']} [{states[node['id']].value}]".replace('"', "'")
        lines.append(f'    {identifier}["{label}"]')
    for node in graph["nodes"]:
        target = f"n_{node['id'].replace('-', '_')}"
        for dependency in node["depends_on"]:
            source = f"n_{dependency.replace('-', '_')}"
            lines.append(f"    {source} --> {target}")
    colors = {
        Status.PENDING: "#d1d5db",
        Status.READY: "#93c5fd",
        Status.BUILDING: "#fcd34d",
        Status.CHECKING: "#fbbf24",
        Status.JUDGING: "#c4b5fd",
        Status.RETRY: "#fca5a5",
        Status.WON: "#86efac",
        Status.BLOCKED: "#ef4444",
    }
    for status, color in colors.items():
        members = [f"n_{node['id'].replace('-', '_')}" for node in graph["nodes"] if states[node["id"]] == status]
        if members:
            lines.append(f"    style {','.join(members)} fill:{color},stroke:#111827")
    return "\n".join(lines)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a resumable graph-based builder and blind-critic loop.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="freeze a reference and initialize a run")
    init_parser.add_argument("spec", type=Path)
    init_parser.add_argument("--run-dir", required=True, type=Path)

    for command in ("status", "ready", "doctor", "mermaid"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("run_dir", type=Path)

    start_parser = subparsers.add_parser("start", help="start or resume a node attempt")
    start_parser.add_argument("run_dir", type=Path)
    start_parser.add_argument("node_id")
    start_parser.add_argument("--workspace", required=True, type=Path)

    build_parser = subparsers.add_parser("record-build", help="record an immutable builder artifact")
    build_parser.add_argument("run_dir", type=Path)
    build_parser.add_argument("node_id")
    build_parser.add_argument("--artifact", required=True, type=Path)
    build_parser.add_argument("--summary", default="")

    checks_parser = subparsers.add_parser("run-checks", help="run a node's deterministic gates")
    checks_parser.add_argument("run_dir", type=Path)
    checks_parser.add_argument("node_id")

    judge_parser = subparsers.add_parser("prepare-judge", help="materialize a blinded A/B packet")
    judge_parser.add_argument("run_dir", type=Path)
    judge_parser.add_argument("node_id")
    judge_parser.add_argument("--seed", type=int, help=argparse.SUPPRESS)

    verdict_parser = subparsers.add_parser("record-verdict", help="map and record a critic verdict")
    verdict_parser.add_argument("run_dir", type=Path)
    verdict_parser.add_argument("node_id")
    verdict_parser.add_argument("result", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            print_json(init_run(args.spec, args.run_dir))
        elif args.command == "status":
            print_json(status_report(args.run_dir))
        elif args.command == "ready":
            print_json(status_report(args.run_dir)["ready"])
        elif args.command == "doctor":
            report = doctor_report(args.run_dir)
            print_json(report)
            return 0 if report["ok"] else 1
        elif args.command == "mermaid":
            print(mermaid_report(args.run_dir))
        elif args.command == "start":
            print_json(start_attempt(args.run_dir, args.node_id, args.workspace))
        elif args.command == "record-build":
            print_json(record_build(args.run_dir, args.node_id, args.artifact, args.summary))
        elif args.command == "run-checks":
            print_json(run_checks(args.run_dir, args.node_id))
        elif args.command == "prepare-judge":
            print_json(prepare_judge(args.run_dir, args.node_id, args.seed))
        elif args.command == "record-verdict":
            print_json(record_verdict(args.run_dir, args.node_id, args.result))
        else:
            raise AssertionError(args.command)
    except BossfightError as exc:
        print(f"bossfight: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
