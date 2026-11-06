# Commit-bound local verification

GitHub owns reviewed source and history. Local checks establish deterministic
engineering evidence; they do not create an independent G2 certificate, provider
qualification or trading authority. GitHub Actions is optional for this program.
Respect every separately enforced repository protection.

## One compatibility authority

[compatibility.json](../src/prop_production/compatibility.json) pins the exact
copytrade-pm and trading-connector revisions, CCXT version and contract/sizing
schemas. The application revision is the Git commit containing that file.
Runtime research access, qualification output, deployment and this verifier consume
that same manifest. Package constraints are checked against it; uv.lock records the
resolved dependency artifacts. No README SHA or historical worktree path overrides it.

Inspect the pins before choosing clean worktrees:

```bash
PYTHONPATH=src python3 -m prop_production.compatibility
export PROP_RESEARCH_ROOT=/absolute/path/to/clean/pinned-copytrade-pm
export PROP_CONNECTOR_ROOT=/absolute/path/to/clean/pinned-trading-connector
bash scripts/verify-local.sh all
```

The script requires a clean reviewed application commit and exact clean companion
commits. It verifies source, tests, docs, lockfiles and untracked project files.
Ignored private runtime/cache paths are not source authority.

The existing entrypoint runs:

1. Python 3.13 and 3.14 application suites, including actual signed SQLite exports
   from the pinned research producer for both fixed-k with absent leader NAV and
   explicit legacy NAV sizing; Ruff, strict mypy, status and synthetic smoke.
2. Application wheel/sdist build and whitespace check.
3. All connector provider tests with the pinned research source available, plus
   connector lint/build. The legacy prospective Combo2 canary is explicitly optional
   when its separate unmerged capture extension is absent.
4. Native Guardian/Propr and Bybit adapter tests in the pinned connector environment.
5. The complete research suite with every extra installed, lint/format, capability
   validation and package build.

The final receipt binds application, research and connector SHAs plus the SHA-256
of the compatibility manifest. Record exact Python/uv versions and pass/skip counts.
Keep the full terminal receipt and its digest in owner-only evidence storage outside
Git. Review the actual diff before merging and repeat relevant gates after changes.

`bash scripts/verify-local.sh app` is useful for application-only checks and does
not establish cross-repository compatibility. `GATE_CLEAN=0` is development-only;
its output cannot be called clean-revision acceptance. Missing gates stay visible.

## Scope and safety

Tests use invented identities, fixture signatures and synthetic broker responses.
The release activation tests exercise content drift, startup/heartbeat failure,
exact pointer/unit restoration, SIGKILL recovery and SQLite WAL-safe backup in
isolated temporary directories. They do not establish a protected systemd deployment.

An Actions billing/job-start failure is not a source failure. A separately required
external check still cannot be bypassed. No test, mode flag or signature permits
broker mutations. Authenticated read-only observation remains a distinct protected
operation with dedicated credentials.

Follower equity/headroom, selected source identity and scope, causal clocks,
reconciliation, actual follower economics, four-week paper evidence and independent
G0–G4 attestations retain their owning gates. Leader NAV is required only by explicit
`ALLOCATION_OVER_LIVE_NAV`, never the default `FIXED_K_NATIVE_QUANTITY`.
