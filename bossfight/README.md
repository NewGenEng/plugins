# Bossfight

Bossfight is a graph-based agent improvement loop. It freezes a real quality bar, breaks work into the fewest independently judgeable nodes, runs isolated builders through deterministic checks, and sends passing artifacts to fresh blind critics. A node unlocks its dependents only after it wins. The integrated artifact is the final boss.

Use it for work where “tests pass” is necessary but not sufficient:

```text
/bossfight rebuild this onboarding flow until it is easier to complete than Linear's current flow.
```

The plugin includes:

- `skills/bossfight/SKILL.md` with the agent workflow.
- `scripts/bossfight.py` with a resumable DAG ledger, deterministic gates, frozen reference digests, and blinded comparison packets.
- `scripts/run-agent-evals.py` with isolated, concurrent, executable agent evaluations.
- `tests/` with state-machine, failure, idempotency, blinding, and CLI integration coverage.

Run the local checks:

```bash
python3 -m unittest discover -s bossfight/tests -v
python3 bossfight/scripts/bossfight.py --help
```

## Design principles

Bossfight keeps the Gauntlet Loop mechanism that earns its complexity: a concrete reference, a separate harsh critic, blind binary comparison, and a win-based exit. It replaces the long generated prompt with an executable graph and makes pstack's principles structural: immutable graph input, per-node write ownership, deterministic checks before judgment, atomic facts, idempotent commands, and focused feedback.

## Credits and changes

The Gauntlet Loop technique was created by Matt Shumer. Jay E at RoboNuggets packaged it as the [Gauntlet Loop skill](https://github.com/robonuggets/gauntlet-loop). Bossfight is an adaptation that runs the work directly, represents it as a dependency graph, adds deterministic engineering gates and resumable state, and includes automated agent evals.

pstack by Lauren Tan informed the engineering principles and workflow shape. Bossfight is independently implemented and does not copy pstack code.

## License

CC BY 4.0. See [LICENSE](LICENSE).
