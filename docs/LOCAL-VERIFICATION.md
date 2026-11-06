# Local verification contract — private VPS

**Effective 2026-11-06:** GitHub Actions is not a required acceptance gate for this private engineering program. The repository uses independently reproducible **local, commit-bound checks**. GitHub remains the source of truth for PRs, issues, review and source revisions. Local test success is not broker authorization, provider certification or a scientific qualification.

## Approved verification entrypoint

Run from any directory with `uv` installed, and with explicit pinned companion worktrees:

```bash
export PROP_RESEARCH_ROOT=/home/quant/.devspace/worktrees/copytrade-pm-dade3502
export PROP_CONNECTOR_ROOT=/home/quant/.devspace/worktrees/trading-connector-829a9992
bash scripts/verify-local.sh all
```

The script must run on a **clean reviewed application commit** and, for `all`, the exact companion revisions. It refuses a dirty source checkout, an incorrect companion SHA, or a missing companion path. It runs the application on Python 3.13 and 3.14, all application tests including the native manifest handshake, Ruff, strict mypy, status/smoke, package build and diff check; then the pinned Propr connector tests and the full research suite and capability validator.

During development only, `GATE_CLEAN=0 bash scripts/verify-local.sh app` is permitted for preliminary feedback. Its output **cannot** be claimed as acceptance of a clean commit. `bash scripts/verify-local.sh app` checks the application alone, and **cannot** establish cross-repository integration.

Store terminal output and the SHA-256 of the completed log in a private, access-controlled directory **outside Git** when documenting a release or issue disposition; include the command, resolved application/connector/research SHAs, Python/uv versions, timestamp, pass/skip counts, reviewer and any gaps. Repeat after each relevant code change. This is local evidence, not an independently signed G2 certificate.

## Operational rules

- Local checks replace the need for GitHub Actions. A GitHub billing/job-startup failure is **not** an engineering blocker and should not be cited as one.
- Do not remove, disable or bypass an independently enforced GitHub branch-protection requirement without a separately authorized repository-settings change. Local verification is not evidence that an external required check passed.
- Do not bypass review: compare the complete PR diff, verify the exact revision, and merge only when actual repository rules allow it.
- Test suites operate in SHADOW with fake broker/credential-free read-only boundaries. No real Propr orders or live capability.
- Missing account NAV, marketability, source receipts, independent accounting, rulebook, simulated fault coverage, native economic tapes, four-week paper evidence, G0–G4 attestations, or actual deployed-service checks remain **BLOCKED**.
