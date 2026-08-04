# autosymph evals

These evaluations measure autosymph at four layers:

1. `trace` — runner-neutral Claude/Codex/Pi tool tracing.
2. `state` — deterministic workflow transitions, claims, and rework exhaustion.
3. `verify` — model decisions over labeled verification evidence packages.
4. `microrepo` — real agent runs against tiny repositories with executable tests.

Run the fast local suites:

```bash
uv run --extra dev --extra tracing python -m evals.run --suite trace --suite state
```

Run every suite and upload versioned datasets plus experiments to Braintrust:

```bash
uv run --extra dev --extra tracing python -m evals.run --suite all --upload
```

The upload command reads `BRAINTRUST_API_KEY` from the environment, falling
back to `~/.autosymph/config/local.env`. Model-provider and tracker credentials
are blanked in agent child processes where they are not needed by the eval.

Pin a Pi model when its configured default provider is unavailable:

```bash
uv run --extra dev --extra tracing python -m evals.run --suite microrepo \
  --microrepo-runners pi --pi-model mlx/my-local-model
```
