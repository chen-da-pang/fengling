# Contributing

Use a PR-first workflow for all non-emergency changes.

## Change Flow

1. Open or reference a GitHub issue describing the problem or requested change.
2. Create a branch from `main`.
3. Make the smallest scoped change that satisfies the issue.
4. Run validation locally.
5. Open a pull request linked to the issue.
6. Review the PR before merging.
7. Merge only after validation and review are complete.

Direct pushes to `main` should be exceptional. If one happens, open an issue
afterwards that records what happened and add follow-up safeguards through a PR.

## Validation

Run these before review:

```bash
make -C scripts/fengling-cli test
python3 ~/.codex/skills/plugin-creator/scripts/validate_plugin.py .
PYTHONPATH=scripts/fengling-cli python3 -m fengling_cli.main --json doctor
```

For setup-related changes, also run an install smoke test with a temporary
`HOME` and `FENGLING_APP_ROOT` so the bundled backend path is verified without
depending on the developer's existing `~/fengling-studio`.
