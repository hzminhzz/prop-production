# Checkpoint SHADOW candidate and compatible code recovery (#327)

Status: engineering rehearsal only. No operational candidate has been signed with trusted production authority. No protected installation, source cutover, account provisioning or broker mutation is authorized.

## Signed prospective candidate

`shadow_release.verify_candidate` verifies a `cwsa_shadow_candidate_v1` envelope under the independently supplied release authority and the `cwsa-shadow-candidate-v1` signature domain. Its required fields bind:

- The original signed `portfolio_release_v2`, all immutable `copy_mandate_v1` generations, account/pod/source/market/risk/rulebook identities and measured source-policy digest.
- The complete compatibility manifest, exact containing application commit, exact research and connector commits, source-proof schema and supported legacy contracts.
- Independently supplied release, mandate and source trust roots. A key named inside an envelope does not become trusted.
- A strictly future UTC five-minute activation boundary shared by its mandates, causal creation time and unexpired release.
- `SHADOW_ONLY` and `trading_authorized=false`.

The explicit verification transition is UNVERIFIED → VERIFY_SIGNED_CANDIDATE → SHADOW_VERIFIED, guarded by exact pins/trust/future boundary, with OBSERVATION_ONLY effect. The original signed bytes are retained in the returned value. Verification reads the existing signer floor and cannot accept a release into it. Boundary equality, wrong account, drifted code/connector/source keys, extra fields and floor downgrade fail.

Synthetic tests sign public fixture evidence. They are not an operational prospective candidate and their millisecond limits are not measured production thresholds.

## Existing worker rehearsal

The existing synthetic Guardian process now exposes checkpoint ingestion/status, signed mandate activation/control/baseline, checkpoint review and fixture-only reservation through its bounded command interface. The same Guardian, financial journal, source receiver and broker IPC own their existing responsibilities. There is no new service or schedule.

A checkpoint fixture requires an explicit logical verification clock. Every financial command retains its own causal clock, signed release check and existing Guardian guards. The worker remains restricted to `synthetic_worker_v1`, fixture-account and synthetic rulebook; its broker is the existing isolated fake process. No authenticated remote execution mode was added.

A checkpoint release refuses the legacy planner. It therefore cannot add the legacy T0+300 delay to a checkpoint. Existing legacy fixtures and installed units retain their contracts. A later operational SHADOW proposal must use approved source/mandate evidence and an expressly reviewed existing-role unit set; publication of these sources does not choose or install that unit set.

`tests/test_checkpoint_worker.py` starts actual isolated worker processes, activates a signed future fixture mandate, records a nonopening baseline, ingests a signed schedule-gap outcome, restarts and checks the same source cursor. It checks account-scope rejection, preserved pre-existing HALT, unchanged finance/fills/positions/intents/reservations, no permit, byte-identical signer floor and an empty broker audit. It also rejects the legacy planner for V2. No native service or account is involved.

Read-only legacy and checkpoint consumers can observe independently without arming either route. One protected opening-effective account/pod generation is enforced by the existing signer floor and immutable mandate lifecycle; replacement does not erase old exposure. Those generation, partial/UNKNOWN and replay tests remain part of the regression suite.

## Named recovery: CWSA_FORWARD_COMPATIBLE_CODE_RECOVERY_V1

Deployment receipts now include a typed code recovery transition: CODE_BOUND, SELECT_CODE, exact manifest proof digest, explicit capability-containment guard, COMPATIBLE or FENCED successor and start-declared-services or preserve-stop effect.

1. Keep original source/financial journals, broker facts, HALTs, reservations, policy versions and signer floors in their existing locations. The recovery API accepts no financial backup to restore.
2. Verify immutable code-bundle inventories and compare durable contract support. Target code must retain every source-wire, release, checkpoint-source and mandate contract supported by the potentially writing candidate.
3. Stop the declared service set before recovery. Compatible recovery changes only the code pointer and service units, then starts only the recorded previous active service set.
4. If older code lacks a retained contract, preserve the current code pointer and new durable state and keep services stopped. Record ROLLBACK_FAILED and the FENCED transition. Do not automatically restart incompatible old code.
5. A separately authorized operator can select a verified compatible release with the existing deployment recovery command's `--target` option. Root ownership/protection and release-root containment are required. Recovery does not lower a signed policy floor or authorize trades.

This is deliberately conservative: a candidate that might have written a newer contract prevents an incompatible fallback even when no such write has been proven. Direct downgrade activation uses the same contract containment rule. Interrupted activation and real SIGKILL recovery remain covered. The tests use temporary directories and recorded service calls; no system service was stopped or installed.

## Evidence and external gates

Red evidence: 11 candidate tests initially failed collection before the verifier existed; three rollback tests demonstrated unsafe old-code restoration/direct downgrade; two actual-worker tests initially rejected the new release at the legacy fixture clock. The implementations satisfy those behaviors. Worker startup failure now closes its child process/pipes, fixing a resource leak exposed by the negative rehearsal.

Exact revisions and the final clean regression receipt are recorded in the program dossier and PR evidence; the compatibility manifest remains the source of companion pins. Reproduce focused evidence with:

```bash
.venv/bin/pytest -q tests/test_shadow_release.py tests/test_checkpoint_rollback.py tests/test_checkpoint_worker.py tests/test_release_integrity.py
```

The real signed future candidate remains blocked on approved measured freshness/skew/grace and capacity limits, new immutable mandate approvals for the selected roster, trusted operational signatures and a future boundary chosen after qualification. Matched prospective economics, native broker capabilities/account-history evidence, protected account permission and explicit deployment/trading authorization are separate gates. None is inferred from this rehearsal. #327 must remain open for the actual trusted prospective-candidate criterion.
