"""Run only with the exact pinned research checkout's Python and test fixtures."""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from copytrade_pm.pm_forward_journal import PaperJournal
from copytrade_pm.pm_forward_paper import PaperCampaign
from copytrade_pm.pm_v4_sixteen_scenarios import IDENTITIES
from copytrade_pm.v4_source_export import export_checkpoint_packet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_checkpoint_paper import BOUNDARY, BTC, GRID, checkpoint, quote, setup_paper
from test_checkpoint_source import scope


def produce(root):
    journal = PaperJournal(root / "paper")
    campaign = PaperCampaign(journal=journal)
    signer = Ed25519PrivateKey.from_private_bytes(b"c" * 32)
    members = {}
    for index, name in enumerate(IDENTITIES):
        _, policy, mandate, paper_policy = setup_paper(
            root,
            run=name,
            account="acct_" + name,
            journal=journal,
            k=str(Decimal("0.001") * (index + 1)),
        )
        members[name] = (mandate, paper_policy)
    baseline = checkpoint(journal, policy, 0)
    from test_pm_forward_ccxt_market import ccxt_market

    catalog = {"BTC/USDC:USDC": ccxt_market()}
    requests = {
        name: {"owner": m.owner, "outcome_id": baseline.outcome_id, "policy": p}
        for name, (m, p) in members.items()
    }
    base = campaign.record_checkpoint_wave(requests, at_ms=BOUNDARY + 102, catalog=catalog)
    packets = [
        export_checkpoint_packet(
            journal, scope(), policy, signer=signer, after_cursor=0, issued_ms=BOUNDARY + 102
        )
    ]
    changed = checkpoint(journal, policy, 1, "3")
    now = BOUNDARY + GRID + 102
    quote(journal, now)
    requests = {name: dict(row, outcome_id=changed.outcome_id) for name, row in requests.items()}
    result = campaign.record_checkpoint_wave(requests, at_ms=now + 2, catalog=catalog)
    packets.append(
        export_checkpoint_packet(
            journal, scope(), policy, signer=signer, after_cursor=2, issued_ms=now
        )
    )
    status = {name: campaign.checkpoint_status(name) for name in IDENTITIES}
    restored = PaperCampaign(journal=PaperJournal(journal.root))
    duplicate = restored.record_checkpoint_wave(requests, at_ms=now + 3, catalog=catalog)
    return {
        "producer_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "financial_mode": "SYNTHETIC_PAPER_NO_BROKER_ORDERS",
        "checkpoint_ms": changed.checkpoint_ms,
        "packets": packets,
        "source_transition_count": len(journal.checkpoint_history(scope(), policy.epoch)),
        "baseline_statuses": [row["status"] for row in base["portfolios"].values()],
        "restart_exact": duplicate == result
        and {name: restored.checkpoint_status(name) for name in IDENTITIES} == status,
        "paper": {
            name: {
                "k": str(m.k),
                "owner": m.owner,
                "signal_id": result["portfolios"][name]["signal_id"],
                "status": result["portfolios"][name]["status"],
                "requested_target": result["portfolios"][name]["requested_targets"][m.owner][
                    BTC.key
                ],
                "quantity": status[name]["account"]["quantities"][BTC.key],
                "costs": status[name]["account"]["costs"],
                "journal_head_sha": status[name]["journal_head_sha"],
            }
            for name, (m, _) in members.items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(produce(Path(sys.argv[1])), sort_keys=True))
