# Guardian and protected source domain glossary

Canonical business meanings. Not an implementation plan or authorization.

- **Financial authority**: Protected Guardian ownership of follower financial state, reservations, HALT, permits and broker reconciliation; distinct from research finance.
- **Domain-owned transition**: A state change whose permitted predecessor, successor and postconditions are verified by the owner of that state.
- **Financial conservation**: Mutual consistency of cumulative fills, orders, reservations, positions, fees and account/pod financial effects; a journal hash alone does not establish it.
- **Unknown execution outcome**: A dispatched order effect that has not been independently reconciled and cannot be safely retried as a fresh order.
- **Recovery evidence receipt**: Independently verifiable, authenticated and incident-scoped proof that one HALT cause has been remediated; a digest-shaped operator assertion is not proof.
- **Recovery requested**: An auditable request to validate remediation, not clearance.
- **Recovery verified**: Verified clearance of a specific HALT cause; the last clearance leads only to DISARMED.
- **Source-notional fixed-k**: Signed selected native position valued at an independent source mark, multiplied by frozen k and converted using an independent follower quote.
- **Source packet**: Authenticated transport envelope containing individual source events, not a global financial commit.
- **Packet completion**: All events in that envelope have reached an accepted terminal processing outcome, distinct from a partially accepted prefix.
- **Protected source projection**: Durable signed source timing/eligibility evidence, not proof of follower holding or broker permission.
- **Consumer cursor**: Durable identity of one consumer's accepted stream prefix, independent of other consumers' acknowledgements.
- **Admissible source mark**: A source-venue valuation with verified account/contract provenance and freshness at the decision cutoff; independent of follower executable pricing.
- **Terminal broker order**: An order whose terminal provider status, actual cumulative executions and complete cutoff evidence agree; residual uncertainty remains financially accountable.
- **Semantic journal replay**: Verification of each committed state change under the invariant rules of its historical event version, distinct from checking SHA-chain integrity alone.
- **Legacy-unverifiable financial event**: A historical event whose available original evidence cannot establish a required present-day financial invariant; not automatically fraudulent, but not a basis for new protected admission.
- **Remediation proof**: Independent source, account or broker evidence establishing the particular HALT cause was corrected; distinct from the operator's authenticated recovery request.
- **Predecessor chain proof**: Authenticated verification that one source occurrence follows the expected previous stream identity, not only that a signer supplied a current chain digest.
- **Consumer effect identity**: A stable association of one original source event to its uniquely owned downstream finance or paper consequence across crashes and retries.
- **Missed due opportunity**: An original new-risk deadline reached without verified execution, which cannot be turned into a fresh signal by source replay.
- **Account-scoped financial HALT**: A latched refusal of new financial exposure on one protected account when its broker/financial evidence is incoherent; it does not imply an unrelated source account has lost continuity.
- **Recovery signer**: A protected role-scoped authority that authenticates a request to verify one incident's remediation, without establishing that remediation has actually succeeded.
- **Signed stream-head anchor**: A trusted per-stream proof of selected scope, epoch, cursor, original chain head and prospective cutoff from which successor continuity is checked; not an attestation of all earlier history.
- **Scoped packet quarantine**: Isolation of a specific invalid source stream inside an otherwise authentic envelope while independently valid streams preserve their accepted progress.
- **Expired new-risk opportunity**: An entry opportunity whose original due instant and authorized grace have passed without verified attempt; no restart or replay makes it fresh.
