"""Public synthetic producer vectors through the actual protected boundary."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_checkpoint_receiver import KEY
from test_copy_mandates import START, mandate_body, release_body

from prop_production.copy_mandates import CopyMandate
from prop_production.evidence import digest, sign
from prop_production.finance import Rulebook
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.judge.broker import FakeBroker
from prop_production.release import ReleaseFloor, ReleaseVerifier
from prop_production.risk import fixture_markets, fixture_policy
from prop_production.v4_source_receiver import SourcePacketReceiver


def quotes(now):
    return {
        m: {"bid": p, "ask": p, "known_at_ms": now} for m, p in (("BTC", "10000"), ("ETH", "1000"))
    }


@contextmanager
def scenario(root, *, copies=((0, "0.025"), (1, "0.01")), gross_limit="20000"):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    vectors = json.loads(
        (Path(__file__).parent / "fixtures/cwsa_checkpoint_vectors_v3.json").read_text()
    )
    authority = Ed25519PrivateKey.generate()
    rules, policy, markets = Rulebook.synthetic(), fixture_policy(), fixture_markets()
    policy["checkpoint_execution"] = {
        "quote_max_age_ms": 2000,
        "account_max_age_ms": 2000,
        "max_gross_notional": gross_limit,
    }
    markets["BTC"].update(qty_step="0.00001", min_qty="0.00001")
    for market in markets:
        markets[market]["source_instrument"] = "hyperliquid/mainnet:perp:default/" + market
    body = release_body(authority)
    body.update(
        risk_policy_sha=digest({"policy": policy, "markets": markets}),
        rulebook_sha=rules.sha,
        checkpoint_policy=vectors["policy"],
    )
    body["members"], body["mandates"], body["checkpoint_scopes"] = [], [], {}
    for index, (source_index, k) in enumerate(copies):
        sc = vectors["sources"][source_index]["scope"]
        pod = "pod-" + str(index)
        m = dict(
            mandate_body(),
            mandate_id="copy-" + str(index),
            pod_id=pod,
            k=k,
            source_id=sc["source_id"],
            source_account_sha=sc["account_sha"],
            source_scope_generation=sc["generation"],
            risk_policy_sha=body["risk_policy_sha"],
            rulebook_sha=rules.sha,
        )
        body["mandates"].append(m)
        body["checkpoint_scopes"][sc["source_id"]] = sc
        body["members"].append(
            {
                "pod_id": pod,
                "source_identity": sc["source_id"],
                "identity_known_at_ms": START - 1002,
                "account_scope": [sc["source_id"]],
                "allocation": "2000",
                "sizing_policy": "FIXED_K_NATIVE_QUANTITY",
                "k": k,
                "allowed_markets": m["allowed_markets"],
            }
        )
    body["definition_sha"] = digest(body["members"])
    body["source_sha"] = digest(
        {
            k: body[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    release = ReleaseVerifier(
        authority.public_key(), "approver", "fixture-account", body["risk_policy_sha"], rules.sha
    ).verify(sign(body, authority, "approver"), now_ms=START - 10)
    floor = ReleaseFloor(root / "floor.sqlite3", initialize=True)
    floor.accept(release)
    journal = Journal(root / "guardian.sqlite3", "fixture-account", initialize=True)
    broker = FakeBroker(root / "broker.sqlite3")
    receiver = SourcePacketReceiver(
        root / "source.sqlite3", release=release, source_key=KEY.public_key(), wire_version=3
    )
    guardian = Guardian(
        journal,
        release,
        floor,
        KEY.public_key(),
        rules,
        policy,
        markets,
        broker,
        mandate_key=authority.public_key(),
    )
    mandates = [CopyMandate.parse(m) for m in body["mandates"]]
    try:
        for m in mandates:
            guardian.activate_copy_mandate(
                sign(m.data, authority, "copy-mandate-v1"), now_ms=START - 1
            )

        def wave(index):
            now = START + index * 300000 + 102
            for source_index in sorted({s for s, _ in copies}):
                packet = vectors["sources"][source_index]["packets"][index]
                receiver.ingest(json.dumps(packet).encode(), now_ms=now)
            return now, {
                m.owner: vectors["sources"][source_index]["packets"][index]["payload"][
                    "transitions"
                ][-1]["effect"]["outcome_id"]
                for m, (source_index, _) in zip(mandates, copies, strict=True)
            }

        baseline_at, _ = wave(0)
        for m in mandates:
            guardian.baseline_copy_mandate(m.owner, receiver, now_ms=baseline_at)
        now, requests = wave(1)
        yield SimpleNamespace(
            guardian=guardian,
            journal=journal,
            broker=broker,
            floor=floor,
            receiver=receiver,
            authority=authority,
            release=release,
            mandates=mandates,
            root=root,
            now=now,
            requests=requests,
            wave=wave,
            policy=policy,
            markets=markets,
            release_body=deepcopy(body),
        )
    finally:
        receiver.close()
        journal.close()
        broker.close()
        floor.close()
