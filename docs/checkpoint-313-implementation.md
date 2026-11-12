# Canonical checkpoint integration — #313

Approved Q1–Q24 design is being implemented on
`integration/cwsa-313-20261112`. Existing installed SHADOW services, release
floor, source wire v1, T0+300 semantics and protected financial history are
unchanged.

## #316: source-only signed delivery

The existing `SourcePacketReceiver`, source handoff and inbox consumer now
support an explicitly selected checkpoint wire v3. Scope and timing-policy pins
are mandatory. The receiver independently checks the domain-separated source
signature, request account/DEX preimages, original native hashes, complete
Decimal vector, actual clocks, economic digest and predecessor/successor chain.
No Guardian financial journal, permit or order is modified.

The source projection uses the receiver's existing source-only database boundary.
Atomic packet commits and immutable file handoff preserve exact source IDs.
`checkpoint_status` and `checkpoint_outcomes` expose source evidence; the
legacy `due` timer does not reinterpret checkpoint signals as T0+300.

Focused verification: 23 tests passed across checkpoint receiver, legacy receiver
and file handoff. Actual SIGKILL before/after commit verifies all-or-none source
progress. Two independent consumers observe one identical CWSA signal ID.
Wrong signer, modified originals, request mismatch, future time, scope drift,
partial chain and conflicting replay fail closed. Later source fencing removes
opening eligibility. Strict mypy passes for the touched source modules.


## #318 — immutable signed Copy Mandates

New `copy_mandate_v1` and `portfolio_release_v2` contracts freeze source scope,
account, pod, generation, k, native markets, timing/risk policy and effective
interval. `FIXED_K_NATIVE_QUANTITY` retains its existing meaning. Activation
waits for the first post-activation complete checkpoint; the baseline has no
financial effect. Pause/retire stop new risk and preserve all prior obligations.
Resume requires a new immutable generation; overlapping active intervals and
rollback are rejected. Source history is independent of follower lifecycle.

Both existing journals persist explicit predecessor/event/evidence/guard/
successor/effect records. Approval signatures and controls are retained and
replayed. Controls must use the originally pinned authority. Guardian rejects
legacy admission for v2 releases. No broker call occurs in lifecycle transitions.

Adversarial TDD found and fixed an unregistered receiver genesis and a foreign
paper-control signer. Receiver restart now revalidates retained signed packets
and requires exact proof coverage for every stored transition. Focused mandate,
source and Guardian verification: PM 18 passed; protected 43 passed; strict mypy
passed. Full integration and prospective evidence remain separate gates.

Compatibility advertises source versions 1/2/3 while `active_source_wire`
remains 1. Exact final companion revision pins and broader integration evidence
will be recorded after the remaining tickets. No source key, trading permission,
protected installation or production cutover is authorized by these changes.

## 2026-11-12 follow-up — market-local reversal fallback

An independent review reproduced a P2 defect: the original execution projection
applied a failed BTC cross-zero predicate to unrelated, otherwise eligible ETH
targets. New Guardian reviews use `checkpoint_execution_projection_v2`:
fallback changes only the affected instrument, keeps per-pod targets and
old-generation residuals, and records the owners whose opposite-side opening
was missed. Reservation and journal validators agree on each owner's
COMMITTED_ONCE versus MISSED outcome. Account-wide reconciliation, gross/pod
risk, independent quotes and native capabilities, immutable deadlines,
reservations and one-order-per-market/checkpoint claims remain mandatory.

The original `checkpoint_execution_projection_v1` algorithm and serialized
shape are preserved only for historical journal replay and previously recorded
decisions; old accounting evidence must never be recomputed using new
projection economics. Regression coverage includes independent BTC/ETH targets,
opposing retained-generation inventory, per-owner disposition tampering,
and original v1 replay compatibility. This change does not deploy protected
code or qualify actual source economics or real trading.
