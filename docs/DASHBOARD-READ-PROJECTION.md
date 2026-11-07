# Restricted dashboard source projection — companion source integration

**Scope:** source-only, no protected worker changes. Companion for
copytrade-pm Phase 2 PR #170 (parent #153; #159, #160, #162).
The running Guardian, planner, broker, observer and watchdog services are
not modified or restarted by this change.

## Authority and data

The only financial authority remains `Journal` owned by Guardian. The added
`operator.project_state` is a *read-only*, bounded, address-redacting projector.
It reuses the existing `operator_status_v1`, `operator_risk_v1`,
`operator_broker_v1`, `operator_portfolio_v1`, `operator_intent_v1` schemas
and exposes additive causal fields:

- Guardian decision ID, proposal ID, first-known, due (first-known + 300s),
  batch-decision and Guardian-decision timestamps. Individual source receipt
  SHA and source-event/source-known/local-first-known clocks are provided
  without source wallet addresses or signed source-envelope bodies.
- Durable intent ID/decision ID, send clock, signed native quantity, actual
  filled accumulator and state; verified original fill IDs are hashed and
  associated with intent ID, market, quantity, price, fee and fill-known clock.
  A provider timeout is still UNKNOWN, not a rejected order.
- Exact owner-computed gross/net broker-mark notional diagnostics only after
  `runtime.certainty=COHERENT`, a usable broker snapshot clock and complete
  position marks. Missing evidence returns UNKNOWN, not zero.
- Guardian exact contractual floors, equity/headroom and cumulative pod
  loss used. The signed *budget value* is not stored in the existing Guardian
  journal, so this exporter does not invent it from a synthetic risk policy.
- Bounded historical `RECONCILE` finance samples from a **full SHA-chain
  verified** snapshot. Each row retains original finance observed clock,
  journal sequence, release identity, equity, unrealized, floor, headroom and
  contractual day. They are `PAPER_SHADOW` source evidence, **not** independently
  broker-executed performance or complete follower economic history.

## Owner-only capture writer

`python -m prop_production.dashboard_capture` is a separate owner process.
The command accepts a protected journal path and an already-created,
owner-controlled capture directory; it reads the entire Guardian hash chain
once and projects all six fixed names from the **same** verified sequence:

```
status.json
risk.json
broker.json
portfolio.json
intent.json
history.json
```

Each file carries `dashboard_guardian_capture_v1`, native
`operator_<command>_v1`, account-binding SHA, journal sequence, release SHA,
source observation clock and capture clock. Every file is capped at 128 KiB.
The writer rejects symlink capture directories, unexpected owners, or
group/world-write access; fixed-name files are installed by fsync +
atomic replace at mode `0640`. No account binding string, wallet address,
credentials, signed source envelope or broker order request is exported.

**Not a production install:** no worker/service is installed, no protected
journal permissions are granted to Marimo, no timer is activated, and no
capture is generated from the live protected account by this PR.
Independent deployment authorization is required for an appropriately
privileged producer and an unprivileged dashboard reader, group ACL,
private ingress, resource limits and rollback.

## Release/operations gates (still required)

1. Owner review of fields/redaction and a pinned companion-compatible app
   release; explicit proof that the installed Guardian release matches.
2. A separate read-only producer OS identity with journal read permission,
   no Guardian write authority; pre-created secure output root with group
   access only for the unprivileged dashboard. Verify `st_uid`, mode, openat
   permissions and no path/symlink injection under the actual OS identities.
3. Measure full-chain export CPU/IO latency and journal-growth effects.
   No UI session may trigger verification. Do not schedule an unbenchmarked
   periodic full-chain scan at a cadence that affects Guardian service.
4. Genuine captured equity/history, broker marks, receipt events and
   release/account parity must pass freshness/schema tests before financial
   presentation. Empty, aged, conflicted or missing owner data remains
   UNKNOWN/UNAVAILABLE/STALE/BLOCKED.
5. Exercise dashboard crash/load isolation, authenticated mobile/browser
   endpoints, resource caps, issuer/recipient group permissions and full
   rollback before any installation.

This extension is intentionally additive to the existing read CLI. Its
`read_model(path, "intent", ..., intent_id=...)` remains capable of exact
single-intent lookups even when the general tail window is truncated.
The read-only exporter cannot place/cancel orders, alter HALT, change
sizing, or mutate the Guardian journal.
