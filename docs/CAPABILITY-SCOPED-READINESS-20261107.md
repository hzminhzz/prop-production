# Capability-scoped readiness — 2026-11-07

The application already separates credential-free `prop-production status`, read-only source observation collection, and independently protected Guardian/financial readiness. Do not introduce a universal readiness flag or infer financial permission from source data.

| Requested capability | Present supported interface | What it proves | Non-prerequisites |
| --- | --- | --- | --- |
| `OBSERVE` | `copytrade-pm` selected-scope public observer and independent health; this application's credential-free `status` remains SHADOW / DISARMED | Verified identity and fresh native scoped source/market receipts when the owning observer actually reports them | Leader whole-wallet NAV, broker Guardian heartbeat, positive research alpha, 72-hour elapsed-time certification |
| `PAPER` **source collection** | `prop-production paper observe` / `ObservationJournal`; `read_only_collection` distinguishes recorded evidence from an empty journal | Durable local receipt hashes and source health only | Historical positive alpha, protected financial worker activation, unrelated regime proofs |
| `PAPER` **follower economics** | Separate simulation/economics qualification | Causal selected-scope simulated fills including k, five-minute policy when configured, quotes, spread, fees, slippage, funding, equity, pending orders and risk | Proof of source whole-wallet NAV in fixed-k mode |
| `DEMO` | Provider classification and G4 account-bound owner-authority checks; currently blocked | Must additionally establish demo endpoint, identity, coherent provider reconciliation, intents, risk and explicit demo-order authority | Unrelated historical portfolio studies |
| `LIVE` | Protected Guardian, active signed release, trusted actual equity/venue constraints and independent risk/recovery; currently unauthorized | Required separately for any real financial mutation | Observability and elapsed uptime alone |

## Existing interfaces do not share admission

- `src/prop_production/paper.py` stores read-only source observations with `broker_mutations=0`, and returns `verdict=BLOCKED_PAPER_ADMISSION` until genuine follower economics and qualification are established. `read_only_collection=EVIDENCE_COLLECTED` states only that receipts were durably recorded. It is not an alpha, paper fill, or account-reconciliation certificate.
- `src/prop_production/qualification.py` implements independent G0–G4 attestations for **protected phase-C progression**, including research G3. These do **not** gate the initial source observer or read-only paper journal collection. Do not delete signed financial attestation merely to make an OBSERVE check pass.
- `src/prop_production/cli.py` `status` without a journal is credential-free and Guardian-independent, returning `account_readiness=UNKNOWN` and `order_route=NONE`, not false broker health.
- The project-wide `scripts/verify-local.sh all` is a **commit-pinned engineering gate** only. It does not grant order authority.
- `copytrade-pm` now isolates selected sealed portfolio observation from unrelated unsigned research priority. Its read-only canary remains subject to source/quote freshness, exact native market identities, journal integrity, rollback and separate operator approval. Optional 72-hour soak may add evidence but is not a fixed prerequisite.

**No protected service, trading authorization, proof or account qualification is changed by this document.** Retain account-bound Guardian heartbeat, high-water mark drawdown, reconciling broker snapshots and independent signing for DEMO/LIVE actions.
