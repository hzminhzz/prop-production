# Deployment and operations runbook — prop-production

Last updated: 2026-11-06.

**Active release**: read `/opt/prop-production/current/COMMIT` (at last writing:
`bd850c4` — docs-inclusive revision, merge lineage `73d8ce2` = PR #36 +
installer corrections + release-manifest validation).
**Rollback target**: the previous verified release — list
`/opt/prop-production/releases/` and use the newest one other than active.
The CLI is authoritative; prose SHAs here go stale the moment a release ships.

## Terminology (do not conflate)

- **Source commit** — the Git commit the release was built from.
- **Git tree** — the content snapshot (`git rev-parse HEAD^{tree}`); two commits
  may share a tree. The installer verifies trees, not just SHAs.
- **Merge commit** — the approved commit on `feat/account-guardian-program` after
  a PR merge. This is the release identity.
- **Installed artifact digest** — the wheel's SHA-256, recorded in
  `releases/<sha>/dist/SHA256SUMS` and verified before installation.

## Deployed layout

- Releases: `/opt/prop-production/releases/<merge-sha>/` — each contains
  `venv/`, `dist/` (wheel + SHA256SUMS), `COMMIT`, `TREE`, `SOURCE.sha256`, `PINS`.
- Active release: `/opt/prop-production/current` (symlink, flipped atomically).
- Durable state (never inside a release): `/var/lib/prop-production/` —
  `guardian/` (journal), `broker/`, `planner/`, `trust/` (settings + floor),
  `observer/`, `watchdog/`, `health/{guardian,observer,watchdog}/` (role-owned
  heartbeats, 0750 dirs, 0640 files).
- Credentials: `/etc/prop-production/observer.env` (0600 root:root, injected via
  systemd EnvironmentFile). Bybit uses a separate `/etc/prop-production/bybit.env`
  (0600 root:root) copied only from the pre-existing dedicated Demo file. Never in Git or logs.
- Bybit private state: `/var/lib/prop-production/bybit/`; heartbeat:
  `/var/lib/prop-production/health/bybit/heartbeat.json` (role-owned, read-only to watchdog).

## Services

| Unit | Runs as | Role |
|---|---|---|
| `prop-production-broker` | `prop-runtime` | synthetic SHADOW broker (socket server) |
| `prop-production-guardian` | `prop-runtime` | Guardian worker + driver (reconcile cadence 4s) |
| `prop-production-planner` | `prop-planner` | Planner worker |
| `prop-production-observer` | `prop-observer` | read-only Propr `propr_native_evidence_v1` polling |
| `prop-production-watchdog` | `prop-watchdog` | independent `watchdog_assessment_v2` evidence |
| `prop-production-bybit-observer` | `prop-bybit` | separate Bybit Demo read-only account/position/order observation |

Broker+Guardian share one uid by the IPC peer-credential contract — a deliberate
temporary limitation; it does NOT establish acceptable isolation for future
authenticated order execution.

## Operator commands

```bash
# Status (authoritative, versioned JSON)
sudo /opt/prop-production/current/venv/bin/python -m prop_production status \
    --journal /var/lib/prop-production/guardian/account.sqlite

# Emergency halt (audited)
sudo ... -m prop_production halt --journal <path>

# Evidence-gated recovery (operator + incident + evidence digest required)
sudo ... -m prop_production recover BROKER_UNREACHABLE --journal <path> \
    --operator <name> --incident <id> --evidence-sha <sha256-of-evidence>
# BROKER_UNREACHABLE additionally requires a fresh guardian heartbeat with
# certainty == COHERENT; the CLI derives the evidence digest from it.

# Deploy a new release (from a checkout matching origin's default branch)
sudo bash deploy/install.sh

# Rollback: repoint and restart (journal and trust floor are never touched)
sudo ln -sfn /opt/prop-production/releases/<old-sha> /opt/prop-production/current.new \
  && sudo mv -T /opt/prop-production/current.new /opt/prop-production/current
sudo systemctl restart prop-production-*
```

## Current operational state (2026-11-06)

- Watchdog: `OPERATIONAL_BUT_BLOCKED` — `market_status: UNKNOWN` (no market
  source connected), guardian `COHERENT`, journal sequence advancing.
- Observer: `propr_native_evidence_v1` live; account bound (`challenge`);
  equity $4,999.225661 (`DERIVED_SDK_FORMULA`); `ledger_cash: UNKNOWN`;
  `RECONCILIATION_UNCERTAIN`; `history_complete: false`; `opening_ready: false`.
- Trading: **never authorized**. `trading_authorized: false` in every surface.
  The connector mutation guard is intact; no real order route exists.

## Bybit Demo (additional read-only venue)

A sixth service, `prop-production-bybit-observer.service`, authenticates the dedicated Bybit Demo key and expected UID against `api-demo.bybit.com`, reads account USD equity/available buying power, supported live positions and open orders, and writes a separate private tape. No trades are sent. Watchdog `bybit_status=OBSERVED_READ_ONLY` means only a fresh authenticated bounded REST read; it does not prove a coherent cutoff, actual available cash, or execution readiness. Run the credential-free tests and protected single-observation smoke described in `docs/BYBIT-DEMO-INTEGRATION.md`.

## Safety invariants

1. HALTs persist across restarts and midnight; only evidence-gated `recover`
   clears them, and never into a falsely reconciled state.
2. The trust floor is root-owned 0440; runtime opens it read-only (DELETE
   journal mode). Only the deploy-time authority advances it.
3. Cross-role heartbeat writes are impossible (verified: observer/planner cannot
   write guardian evidence; guardian cannot write watchdog assessments).
4. A failed trust migration aborts the install and preserves the previous
   database; sidecars are removed only after proven success.
5. Missing market/source evidence is never reported as healthy.
