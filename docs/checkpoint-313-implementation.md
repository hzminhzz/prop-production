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

Compatibility advertises source versions 1/2/3 while `active_source_wire`
remains 1. Exact final companion revision pins and broader integration evidence
will be recorded after the remaining tickets. No source key, trading permission,
protected installation or production cutover is authorized by these changes.
