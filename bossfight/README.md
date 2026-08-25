# Bossfight

Bossfight is a graph-based agent improvement loop. It freezes a real quality bar, breaks work into the fewest independently judgeable nodes, runs isolated builders through deterministic checks, and sends passing artifacts to fresh blind critics. A node unlocks its dependents only after it wins. The integrated artifact is the final boss.

Use it for work where “tests pass” is necessary but not sufficient:

```text
/bossfight rebuild this onboarding flow until it is easier to complete than Linear's current flow.
```

It is the best of both worlds between [Gauntlet Loop](https://github.com/robonuggets/gauntlet-loop) and [pstack](../pstack/): Gauntlet Loop's concrete bar, fresh blind critic, and win-based exit, combined with pstack's smallest-change discipline, isolated write scopes, and deterministic verification before judgment.

## Getting started

Bossfight works in Cursor, Claude Code, and Codex. Requirements: `python3` (3.10+) on your `PATH` and `git` if you want per-node worktree isolation.

### Cursor

Install from the plugin marketplace (search for "Bossfight"), or add this repo as a marketplace source. The skill is picked up automatically; invoke it explicitly or let the agent pull it in:

```text
/bossfight make this CLI's error messages clearer than ripgrep's.
```

### Claude Code

Add this repo as a marketplace and install the plugin:

```text
/plugin marketplace add cursor/plugins
/plugin install bossfight@cursor-plugins
```

The `bossfight` skill then loads whenever you ask Claude to "bossfight" or "gauntlet" a task, or invoke it directly with `/bossfight`.

### Codex

Install from this repo (the `.codex-plugin` manifest is included):

```bash
codex plugin install cursor/plugins --plugin bossfight
```

Then use `$bossfight` in a prompt, or just describe a task with a quality bar and let implicit invocation pick it up.

### Your first fight

1. Pick a goal with a real, fetchable reference to beat — an existing implementation, a competitor's page, a published article. Vague bars are rejected by the skill.
2. Ask your agent to bossfight the goal. It freezes the reference digest, builds the smallest dependency graph, and starts the ready frontier.
3. Watch the ledger (kept in `/tmp/bossfight-<slug>/` by default). Each node must pass its deterministic checks, then win a blind A/B comparison against the reference, before its dependents unlock.
4. The run ends only when the final integrated artifact wins, you stop it, or the agent reports a hard block with evidence. Runs are resumable: point the agent at the same ledger to continue.

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
