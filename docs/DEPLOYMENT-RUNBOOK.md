# Deployment and recovery — prop-production

## Authority and current state

The reviewed default branch is `feat/account-guardian-program`. Inspect GitHub's
actual default branch before a release. The single
[compatibility manifest](../src/prop_production/compatibility.json) binds companion
commits and schemas; [local verification](LOCAL-VERIFICATION.md) binds that manifest
to the containing application commit.

`/opt/prop-production/current/COMMIT` identifies the selected installed artifact.
It alone cannot prove which code a running PID loaded. Verify the complete bundle,
service identity, process start and fresh role evidence. Dated checkpoint reports
are historical; do not copy their account values or SHAs into this guide.

Default SHADOW / DISARMED / NO_TRADE. A deployment never authorizes orders.

## Immutable bundle

New releases live at `/opt/prop-production/releases/<approved-commit>/` and contain:

| Artifact | Purpose |
| --- | --- |
| COMMIT / TREE / SOURCE.tar / SOURCE.sha256 | Exact approved Git identity and archived source |
| source/ | Build source extracted from that Git object |
| compatibility.json | Copy of the authoritative commit-bound contract manifest |
| connector/ | Tracked connector source at the exact companion commit, including native SDK |
| venv/ / dist/ | Non-editable frozen dependencies and built application wheel |
| units/ | The five unit definitions that belong to this code bundle |
| BUNDLE.json | Derived hashes, modes and symlink targets for the complete installed code set |

Runtime provider PYTHONPATH points inside `current/connector`. No deployment
mutates a shared connector directory. Older releases may still use the legacy
shared directory; retain it until no service or rollback depends on it.

The bundle receipt is generated evidence, not a second configuration authority.
Verification rejects changed/missing/added code, wrong commit, altered contracts,
partial artifacts and an unavailable interpreter. Runtime bytecode caches are
excluded from code identity. Root owns protected code and its resolved interpreter.

## Protected installation

Complete the clean commit-bound `bash scripts/verify-local.sh all` gate first.
This operation requires root deployment authority and dedicated existing read-only
provider credentials. If root access is unavailable, stop only the protected
installation; continue source verification and retain the running release.

From the exact merged application checkout, supply the clean pinned connector:

```bash
sudo env PROP_CONNECTOR_ROOT=/absolute/path/to/pinned-trading-connector \
  bash deploy/install.sh "$PWD"
```

The installer fetches the approved default branch and requires exact HEAD equality
and clean app/connector source. Fetch failures are fatal. It checks Demo/read-only
credential scope without printing values, and requires a root-owned Python >=3.13.
It archives Git objects, builds a non-editable frozen environment, verifies the
bundle, and preserves the previous release before activation. An incomplete or
changed existing release is preserved for inspection; it is not deleted or reused.

The installer takes a deployment lock. Activation snapshots the previous pointer,
unit bytes and active-service set to root-private recovery storage, then stops
exactly the five listed services. It installs the candidate units, switches the
pointer atomically, starts them, checks activity and requires fresh Guardian,
Propr, Bybit and watchdog heartbeat files. A failed step restores the exact old
pointer/unit set and starts only the previously active services. Financial journals
and the monotonic trust floor are never rolled back.

The trust floor must already use DELETE journaling. Legacy WAL conversion is a
separate reviewed offline migration; the installer never checkpoints inside a
transaction, raw-copies a live WAL database, or deletes sidecars. Its pre-upgrade
trust backup uses SQLite's online backup API and independent integrity checks.

## Verification and rollback

Verify a named installed release using reviewed source:

```bash
PYTHONPATH=src python3 -m prop_production.deployment verify \
  /opt/prop-production/releases/APP_SHA --commit APP_SHA
systemctl show prop-production-guardian.service \
  -p User -p Group -p MainPID -p ActiveState -p SubState -p NRestarts -p ExecMainStartTimestamp
```

Bundle receipts and private health evidence may require root read access. Check all
five exact units, their journal errors and fresh role evidence. The Guardian account
journal has a separate read-only status surface:

```bash
sudo /opt/prop-production/current/venv/bin/python -m prop_production status \
  --journal /var/lib/prop-production/guardian/account.sqlite
```

A process crash during activation leaves a durable PREPARED record. A new activation
refuses to proceed until that exact previous state is recovered:

```bash
sudo env PYTHONPATH="$PWD/src" /usr/bin/python3 -m prop_production.deployment \
  recover /opt/prop-production
```

For an intentional rollback, choose a **named, verified, schema-compatible** bundle
and use the same activation command. Never choose the newest directory by timestamp:

```bash
sudo env PYTHONPATH="$PWD/src" /usr/bin/python3 -m prop_production.deployment \
  activate /opt/prop-production/releases/VERIFIED_ROLLBACK_SHA
```

Manual rollback requires the target's bundle receipt and compatible journal/trust
contracts. The automatic failed-upgrade recovery restores the captured prior code
and units, including a legacy currently running release, without rewriting its files.
A successful code rollback does not clear HALT, reverse fills or lower the trust floor.

## Retiring a predecessor behind the code fence

When `activate` of a candidate fails with `ACTIVATION_INCOMPATIBLE_DURABLE_CONTRACTS`,
the units stay stopped and `code-fence.json` is written. The fence is the default and
is never lifted implicitly. With the services stopped (a live journal has a WAL and
fails verification), an operator may record that the predecessor is safe to retire:

```bash
sudo env PYTHONPATH="$PWD/src" /opt/prop-production/releases/CANDIDATE_SHA/venv/bin/python \
  -m prop_production.deployment retire-predecessor /opt/prop-production \
  --candidate /opt/prop-production/releases/CANDIDATE_SHA \
  --journal /var/lib/prop-production/guardian/account.sqlite
```

It proves: `current` is the predecessor, both bundles verify and are SHADOW with
`trading_authorized` false, the pair is genuinely `FENCED`, no activation is
interrupted, and the journal is either absent or a legacy v1 chain that verifies
read-only (hash chain, snapshot, no semantic-v2 rows). The result is a root-private
`retirements/PRED__CAND.json` (`predecessor_retirement_v1`, self-digested).

`activate` then accepts the receipt only if its digest, the transition evidence and a
fresh journal re-check all still match; otherwise the fence behaves exactly as before.
It never changes `current`, units, journals or `code-fence.json`; only a successful
activation clears a fence written for the same transition and records
`retired_predecessor_receipt` in `activation.json`.

## Service and state ownership

| Unit suffix (prop-production-) | Identity | Responsibility |
| --- | --- | --- |
| broker | prop-runtime | Synthetic SHADOW broker/socket server |
| guardian | prop-runtime | Guardian finance, durable intent and risk authority |
| observer | prop-observer | Read-only Propr native account evidence |
| watchdog | prop-watchdog | Independent operational assessment |
| bybit-observer | prop-bybit | Dedicated Bybit Demo read-only observation |

Broker and Guardian share one uid under the existing IPC contract. This is not a
claim of acceptable isolation for future authenticated order execution.

Durable financial state stays under `/var/lib/prop-production/`. Guardian, observer
and Bybit heartbeats are under `health/<role>/heartbeat.json`; watchdog evidence is
under `watchdog/`. Role-owned directories and root-protected trust permissions
remain mandatory. Root-only credential files stay under `/etc/prop-production/`.
Never publish their contents or private account responses.

Missing market/source evidence can leave the watchdog OPERATIONAL_BUT_BLOCKED even
when all processes are active. Provider inventory is not coherent fill history or
trusted ledger cash. Preserve RECONCILIATION_UNCERTAIN, G0–G4 blocks and NO_TRADE.
