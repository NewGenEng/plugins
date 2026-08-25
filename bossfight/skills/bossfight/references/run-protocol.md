# Run protocol

Use `python3 <plugin-root>/scripts/bossfight.py`. The script keeps the graph immutable and derives state from per-node attempt facts. Builders never write the shared graph. The coordinator is the only actor that records transitions.

## 1. Capture the bar and write the graph

Capture the reference as a local file or directory. Write a spec like this:

```json
{
  "schema_version": 1,
  "goal": "Ship a JSON log formatter whose output and ergonomics beat the reference.",
  "bar": {
    "name": "Named formatter release",
    "source": "https://example.com/named-formatter",
    "artifact": "reference-capture",
    "question": "Which artifact is faster to understand and more reliable on the declared cases?"
  },
  "nodes": [
    {
      "id": "parser",
      "title": "Parse the supported log shapes",
      "depends_on": [],
      "compare": false,
      "checks": [
        {
          "name": "parser tests",
          "argv": ["python3", "-m", "unittest", "tests.test_parser"],
          "cwd": ".",
          "timeout_seconds": 120
        }
      ]
    },
    {
      "id": "cli",
      "title": "Build the user-facing command",
      "depends_on": ["parser"],
      "compare": true,
      "checks": [
        {
          "name": "CLI tests",
          "argv": ["python3", "-m", "unittest", "tests.test_cli"],
          "cwd": "."
        }
      ]
    },
    {
      "id": "final",
      "kind": "final",
      "title": "Judge the integrated formatter",
      "depends_on": ["cli"],
      "compare": true,
      "checks": []
    }
  ]
}
```

Rules enforced by the CLI:

- IDs are lowercase kebab-case and unique.
- Dependencies exist and the graph is acyclic.
- There is exactly one final node. It is a sink and depends on every terminal work node.
- Each work node has a deterministic check, a comparison, or both.
- Check commands are argument arrays. They never pass through a shell.
- Check working directories are relative and cannot escape the builder workspace.
- Blind opponents share the same material shape. A file compares with the same file type, and a directory compares with a directory.

Initialize the run:

```bash
python3 scripts/bossfight.py init graph-spec.json --run-dir /tmp/bossfight-logfmt
python3 scripts/bossfight.py status /tmp/bossfight-logfmt
```

Initialization copies and fingerprints the bar. Re-running the same initialization converges on the existing run. A changed spec or changed source bar is rejected.

## 2. Work the ready frontier

List ready or retryable nodes:

```bash
python3 scripts/bossfight.py ready /tmp/bossfight-logfmt
```

For each node, create an isolated builder workspace, then start the attempt:

```bash
python3 scripts/bossfight.py start /tmp/bossfight-logfmt parser --workspace /tmp/worktrees/parser
```

`start` prints the attempt path and prior feedback. If an unfinished attempt exists, it returns that attempt instead of duplicating it.

After inspecting the builder's output, record the directly judgeable artifact. The artifact must live inside that builder's workspace.

```bash
python3 scripts/bossfight.py record-build /tmp/bossfight-logfmt parser \
  --artifact /tmp/worktrees/parser/proof/parser-demo.txt \
  --summary "Parser handles every declared input shape."
python3 scripts/bossfight.py run-checks /tmp/bossfight-logfmt parser
```

Recording copies the artifact into the attempt as immutable evidence. Checks reject workspace edits made after that snapshot. Later retries may change the workspace without corrupting earlier attempts. A failed check makes the node retryable, and the next `start` creates a fresh attempt containing the failed evidence.

## 3. Blind the critic

For a comparison node whose checks pass:

```bash
python3 scripts/bossfight.py prepare-judge /tmp/bossfight-logfmt cli
```

The command materializes randomized `A` and `B` artifact directories and emits `judge-request.json`. It writes no identity key. The recorder later derives the mapping from the frozen digests, so there is no secret file for a critic to discover. Give the critic only that request and its two labeled paths. The critic returns:

```json
{
  "winner": "A",
  "biggest_gap": "The losing artifact hides its error recovery path.",
  "evidence": ["Case 4 exits without a usable correction hint."]
}
```

Record it:

```bash
python3 scripts/bossfight.py record-verdict /tmp/bossfight-logfmt cli critic-result.json
```

The CLI maps the blind label back to `ours` or `bar`. `bar` and `tie` feed the named gap into a new attempt. `invalid` blocks the node until the comparison or frozen bar is repaired.

## 4. Finish and audit

Integrate won nodes through the single coordinator. Then work the final node against a whole-artifact capture. A component win never substitutes for the final win.

Useful views:

```bash
python3 scripts/bossfight.py status /tmp/bossfight-logfmt
python3 scripts/bossfight.py mermaid /tmp/bossfight-logfmt
python3 scripts/bossfight.py doctor /tmp/bossfight-logfmt
```

`doctor` rechecks the frozen bar, graph invariants, recorded artifact digests, and attempt shapes. Run it before declaring completion.
