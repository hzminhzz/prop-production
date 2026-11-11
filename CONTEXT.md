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
