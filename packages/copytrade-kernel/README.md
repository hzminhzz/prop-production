# copytrade-kernel

This package provides pure checkpoint decisions for Guardian, virtual paper, and research. It installs independently of `prop-production` and has no runtime dependencies outside Python 3.13+.

## PM03 integration

- Parse signed mandate payloads with `CopyMandateSpec.parse(payload)`. `spec.data` retains the original `pod_id` name, decimal string, and canonical fields. Calculate the mandate and owner hashes in the caller using its evidence layer.
- Build `CheckpointBatch[ProspectiveCWSA]` from verified CWSA outcomes. Each `SourceOutcome` carries an immutable owner ID, `FixedKSizing`, publication and deadline clocks, `SourceLeg` records, and the source cursor and digests. Use `RetrospectiveArchive` for backtest input to the same kernel.
- Supply `KernelState` with owner-local positions and `OpeningMissed` memory. Retain the signed quantity at the last missed eligible opening. A same-sign decrease or rebound at or below that quantity cannot replace it. Record a new miss after a genuine increase or flip if the attempt misses. Clear a miss when owner attribution proves entry.
- `step_checkpoint(state=..., frame=...)` returns fixed-k notionals and per-owner rejected native instrument keys. The shell supplies publication/deadline checks, source completeness, and risk/account evidence. An optional `DecisionInputs` runs exact `size_book` and V2 net execution projection in the same call. The result contains immutable risk and plan records, and account-level `NetOrderIntent` values. `.data()` returns independent plain dictionaries for legacy receipt serialization.
- `DecisionInputs` takes an account state with cash/risk fields, net and owner positions, and reconciled entry basis. Market context carries exact quote and lot inputs and shell-verified capabilities. `RiskPolicy` carries the effective policy chosen by the caller. Use `FrozenObject.parse()` for receipt-shaped dictionaries. For a virtual account, supply paper-owned evidence and preserve `order_route=NONE` in the paper shell.
- `RulebookCatalog.get(key)` and `RulebookCatalog.list_keys()` return canonical challenge terms. `account_step` accepts `AccountValuation`, the prior finance record, and a rulebook digest produced by the caller.

Guardian journal rows created before cutover replay the existing semantics with their recorded projection version. New reviews persist `checkpoint_decision_q15_v1` independently of source wire V3. The Guardian shell owns signatures, authorization, reservations, dispatch, and recovery.
