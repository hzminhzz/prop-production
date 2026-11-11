# ADR: Guardian invariant-enforcement grill, Round 1

Date: 2026-11-11
Status: Owner-approved design Q1=B, Q2=A, Q3=B, Q4=B; documentation only, no code or runtime authorization.

## Context
The cross-repository audit found that Journal.commit verifies account identity, writer sequence and hash integrity but does not universally prove financial transition legality. Journal.recover accepts operator/incident strings and a syntactically valid evidence digest without intrinsically verifying remediation. BrokerSnapshot.ready does not enforce FILLED implies full signed executed quantity. V4 projection reads predecessor cursor before BEGIN IMMEDIATE; its packet receiver may commit an accepted prefix before packet completion. No actual concurrency exploit or real broker misreport was reproduced. Protected V4 remains source-only SHADOW, NO_TRADE.

## Q1: Domain-owned protected financial transitions (APPROVED, option B)

Guardian alone owns protected financial state and permissions. Validate permitted transitions, immutable account/intent identities, reservation and fill conservation, HALT latching, outstanding unknown outcomes, release and permit epochs and reconciled account/pod finance at the owning journal boundary. A hash-valid snapshot is not automatically semantically valid. Replay checks the historical transition versions, preserving signed history and nonrewindable journal/trust floor.

Retain existing finance journal, single-writer fencing and network calls outside SQLite transactions; do not build a second global state-machine/ledger, delegate financial authority to research Pydantic models, or use monitoring as sole enforcement.

## Q2: Source-notional fixed k (APPROVED, option A)

Approved fixed-k policy: follower target USD notional equals frozen k times source signed native quantity times a separately proven source mark. Independent, fresh follower quotes determine target native quantity; native precision and Guardian risk admission still apply. The FIXED_K_NATIVE_QUANTITY identifier retains this meaning, not simple multiplication of native quantities when leader/follower prices differ. Leader whole-wallet NAV is not a universal gate. No fallback to reused follower midpoints, source marks as broker bid/ask, or Vike OHLC as an executable quote.

Source observation is only eligibility, never broker permission.

## Q3: Incident-scoped authenticated HALT recovery (APPROVED, option B)

A syntactically valid evidence digest is insufficient. A recovery receipt must authenticate the responsible operator/reviewer authority, bind the specific account, incident, HALT cause, relevant journal/epoch state, expiry and verifiable remediation evidence. Verify cause-specific recovery predicates and independent provider/financial observations when applicable. A receipt cannot clear unrelated HALTs or be replayed across accounts/incidents.

Record requested versus verified recovery phases where consequential. Preserve existing narrowly scoped MIDNIGHT_BASELINE_UNKNOWN reset behavior; neither a skipped day nor an arbitrary operator string clears a finance problem. Successful clearance revokes permits, advances the boot epoch and only DISARMS; it never re-arms or dispatches orders. Avoid a blanket two-person approval rule for routine SHADOW remediation; precise trust/receipt schema is deferred to Round 2.

## Q4: Per-event V4 proof and independent effects (APPROVED, option B)

A signed packet is a transport envelope. For each event, verify signed source identity/scope, original payload, epoch/cursor and predecessor chain relationship. Commit source progress and projection atomically at the owning SQLite boundary; lock or CAS the predecessor inside the write transaction. Treat exact replay as idempotent and conflicting identity as divergence.

Partial packet progress is permissible but must be explicit and replay-safe; completion marker means all events accepted, not merely that some prefix was committed. Historical signed chain contracts must not be reinterpreted when extending predecessor evidence. Protected source projection remains separate from Guardian financial permits and cannot claim a fill.

Rejected: global packet/financial transaction, globally shared consumed cursor, automatic trading enablement.

## Open Round 2
Broker order terminal/fill/reservation consistency; semantic journal replay policy; signed recovery authority and remediation witness; source chain backward compatibility; source packet partial progress, interleavings and crash/restart proofs. These are unresolved design choices, not declared test passes.

Relevant sources: src/prop_production/journal.py, guardian.py, gateway.py, v4_timing.py, v4_source_receiver.py; docs/V4-PROTECTED-SOURCE-OPERATIONS-20261109.md and docs/SIZING-POLICY.md.
