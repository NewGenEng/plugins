# Agent evals

Test observable behavior, not claims that the agent followed Bossfight.

## What to measure

- The organic task reaches its executable acceptance checks.
- A run ledger exists and passes `bossfight.py doctor`.
- The reference is concrete and frozen before the first build.
- Builder and critic artifacts are separated.
- A losing verdict produces a new attempt with focused feedback.
- The final node wins before the agent reports completion.

Do not ask candidates to name principles, quote the skill, or explain the evaluation. Those cues change behavior. Candidate directories use neutral project names. When comparing variants, hide variant and model identities from the judge.

## Suite format

`scripts/run-agent-evals.py` accepts standard-library-only JSON:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "orbit-notes",
      "fixture": "fixtures/orbit-notes",
      "prompt": "Bossfight this: add deterministic export to the notes CLI and prove it from the user surface.",
      "protected": ["reference-contract.json"],
      "holdout": ["tests"],
      "checks": [
        {
          "name": "acceptance",
          "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
          "cwd": ".",
          "timeout_seconds": 120
        }
      ]
    }
  ]
}
```

The runner copies each fixture to a sanitized temporary workspace, removes holdout paths, launches the candidate command with the organic prompt on standard input, restores holdouts, and runs every check without a shell. Protected files are fingerprinted before the run and fail the result if changed. The runner writes `report.json` plus full stdout and stderr. Runs are isolated and may execute concurrently.

Example with Codex:

```bash
python3 scripts/run-agent-evals.py evals/suite.json \
  --runner 'codex exec --ephemeral --skip-git-repo-check -s workspace-write -C {workspace} -' \
  --jobs 2 \
  --output /tmp/bossfight-agent-runs
```

Use `--prompt-prefix` to point a candidate at a development skill that is not installed. Keep the organic case prompt unchanged. Avoid production credentials and external mutations in eval fixtures.

Promote a change only when the executable checks and ledger audit improve or stay green across repeated runs. Read the produced artifacts and transcripts before accepting a judge's summary.
