# ADR: Guardian invariant grill Round 5 — proof-based acceptance and nonrewinding release

**Date:** 2026-11-11
**Status:** OWNER APPROVED FOR DESIGN — Q26=A, Q27=A, Q28=A, Q29=A, Q30=A, Q31=A, Q32=A, Q33=A. Documentation only. No code, protected keys, journal mutation, push, merge, deployment or trade authority.

## Context and central abstraction

Extends [Round 1](ADR-INVARIANT-ENFORCEMENT-GRILL-20261111.md), [Round 2](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND2-20261111.md), [Round 3](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND3-20261111.md) and [Round 4](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND4-20261111.md).

**Proof-carrying domain-owned transitions** are the unifying architectural rule: a signed source observation, provider report or recovery request is evidence, not authority. An independently authorized Guardian reducer checks its own financial preconditions, invariant proof and current journal head before committing a protected effect or a durable rejection. A financial journal receipt is distinct from a provider ACK or source packet delivery. This is a bounded logical pattern reusing existing Guardian, SQLite, Pydantic trust boundaries and compatible wire schemas — **not** a new central state machine or second accounting authority.

## Q26 — Critical adversarial proof, not a ceremonial checklist (APPROVED: A)

Close the audited residual findings with evidence at their owning trust boundaries:

- Causal source/mark: two-writer stale-cursor race, authentic predecessor links, reordering, missing/stale leader price, scoped invalid stream, missed T+300 and replay.
- Financial safety: FILLED/partial/unknown order conflict, fill/cancel race, reservation conservation, HALT latching, signer revocation/forgery/expiry, recovery request with still-unresolved broker evidence.
- Persistence: before/after local commit crash, lost receipt acknowledgment, packet PARTIAL, deterministic consumer identity and no duplicated modeled/Guardian effect.
- Integration: signed producer/receiver/connector exact versions, incompatible wire rejection, protected source-only NO_TRADE and rollback with journal/trust floor unchanged.

Re-use prior engineering cases when they directly test the same behavior, but test each new failing boundary with positive and negative oracles and combined-fault stress. No claim of passed tests is made by this ADR. Do not add unrelated acceptance gates to an additive documentation/research merge.

## Q27 — Empirical source-mark windows (APPROVED: A)

Use independently authenticated observed leader-source marks and original clocks to establish venue/native-market-specific age, update latency, skew, missingness and fallback distributions at the actual decision cutoff. Bind approved max ages and data trust to a future versioned source-mark policy. No single made-up universal seconds limit or follower price fallback. A missing authenticated mark blocks that new-risk operation; it does not stop read-only source collection.

## Q28 — One compatibility authority and exact clean revisions (APPROVED: A)

Keep src/prop_production/compatibility.json as sole producer/consumer/connector revision and schema pin authority. A protected candidate must pass relevant signed-source, finance, provider status, recovery and failure-case tests on clean commits, mandatory dependency environments and both supported application Python versions where the existing gate requires them; required skips are blockers, not passes. Keep artifact hashes/test receipts owner-private. Cross-repository engineering success is **not** independent provider coherent cutoff or trading authorization.

## Q29 — Scope-bounded authentic source SHADOW canary (APPROVED: A)

With separate installation approval, verify a signed anchored baseline and genuine post-baseline changes, original T0, eligible/expired T+300, independently fresh marks, repeated input and restart, scoped source fault and evidence freshness. Prove zero source-to-order mutation route. This is source-only SHADOW acceptance, not synthetic account financial admission or positive alpha. No blanket four-week study or 72-hour soak is required for this specific read-only capability.

## Q30 — Versioned rollout and compatible nonrewinding rollback (APPROVED: A)

Verify and stage each boundary separately: quant signed source producer, independently pinned protected source-only receiver, simulated paper destination and **separately authorized** protected Guardian/broker admission. Reuse docs/DEPLOYMENT-RUNBOOK.md and the immutable bundle/rollback contract. Roll back named compatible **software artifacts and units**, never original source receipts, account financial journal, trust floor, outstanding order, or HALT. Stop affected new risk if no code rollback version can understand durable v2 state. Unit activity and a symlink are not actual running revision proof. Never auto-enable broker mutations from a protected source canary.

## Q31 — Bounded operational SQLite, retained original proof (APPROVED: A)

Keep immutable original packets, receipt identities and unresolved financial evidence in protected private storage under a separately evidenced retention policy. Operational databases need compact verified cursors, unique effect IDs, packet PARTIAL/COMPLETE/RESOLVED_WITH_QUARANTINE state, account-level HALT and bounded diagnostics. Compaction cannot discard unsettled orders, invalid wallet source proof, unresolved packets or original signed bytes needed to audit them. Measure WAL/file growth before claiming disk remediation.

## Q32 — Historic signature proof versus active authority (APPROVED: A)

Keep retired public keys to verify old immutable receipts at their original trust level, but require new independently approved signer pins and an authenticated epoch/head handshake for future source events and recovery requests. Revoked signers must not authorize new events even when historical signatures remain valid. Test revoked and wrong-scope keys, replay, rollback and version downgrades. Distinct source and recovery signer roles remain distinct from release approver and broker credentials.

## Q33 — Operational budgets from measured behavior (APPROVED: A)

Measure valid and faulted event rates, protected receiver lag, recovery time, CPU/memory, disk/WAL growth, SQLite contention and quarantine/backlog under realistic VPS resources. Define bounded queues, lock/timeout semantics, retention and failure escalation from observed constraints. A resource or latency violation blocks only its **operational release stage**; it does not invalidate unrelated read-only research or create an automatic financial permit.

## Summary and residual factual unknowns

All five grill rounds settle the relevant **architectural decisions**; no further owner choice is needed for per-field naming, arbitrary threshold values or routine test fixture details. Exact source-mark age values, coherent broker history coverage, protected signer provisioning, real deployment revision, and runtime resource budgets require evidence, not conversation approval.

Gate separation is mandatory:
1. Code review/merge: verified additive engineering with no protected side effects.
2. Read-only authenticated SHADOW: independently approved bounded deployment and source proof.
3. Protected finance/DEMO/LIVE: complete account-bound financial reconciliation, original risk/signed release and explicit independent operational authorization.
4. Scientific follower qualification: actual prospective executable evidence, independent of all the above.

This ADR does not alter src/prop_production/compatibility.json, code, trusted signer pins, account journals, release floor, systemd units or broker routes.
