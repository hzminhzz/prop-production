# Engineering evidence and remaining gates

This is an evolving program, not a readiness approval.

## Published changes

- Bootstrap: 60ed18ab3bb5239bf9b36db1ce118469095fe8f8.
- Release, journal and read-only gateway: 90001cd593d6122ceb19c97d1100f77c3f62bd8c.
- Connector: ec3f03a5d24a2db3b0c75fe61e3a3fbfa662b343, trading-connector PR 18.
- Application integration: prop-production PR 23 (draft).

## Verified locally so far

- 36 behavioral tests: canonical signed release and rollback checks, journal
  integrity/single writer/backup/SQLite lock and injected disk errors, causal
  300-second planner, exact account floors, native quantity sizing, independent
  Guardian proposal validation, commit-before-send and accepted-after-timeout
  reconciliation.
- Connector's Propr suite: 138 passed, 2 skipped. Remote mutation paths fail closed.
- Native research exporter focused tests: 3 passed in isolated copytrade-pm worktree.
  The real registry export is BLOCKED: source account scope/NAV attestations and
  some point-in-time identity provenance are missing. No registry was changed.

## External blockers

- GitHub Actions run 42096201254 did not start: account locked due to a billing
  issue. This is not green CI. Do not bypass protected checks.
- Non-interactive sudo cannot authenticate. Separate root-provisioned service
  identities and protected system units have not been installed.
- Real account terms, margin/lot evidence and provider coherence/demo semantics
  are not authenticated.
- No live authorization. No real broker mutation route.
- Four weeks of prospective paper evidence and a qualified cost-complete
  independent holdout do not yet exist.
- Independent signed safety/readiness approvals do not yet exist.

## Current implementation boundary

Synthetic requests can be verified, sized, reserved, dispatched to a persistent
fake broker and reconciled. This is engineering evidence only. The remaining
program includes real process crash/fault verification, OS isolation packaging,
operator surfaces, prospective evidence intake, native research economics and
G0–G4 dossier. No implementation ticket has been closed prematurely.

Status vocabulary: IMPLEMENTED, ENGINEERING_VERIFIED, DEPLOYED, RUNNING_OBSERVED,
SCIENTIFICALLY_QUALIFIED and TRADING_AUTHORIZED are independent fields.
