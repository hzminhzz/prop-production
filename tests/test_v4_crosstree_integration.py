"""Real 3.14 source producer -> signed export -> 3.13 protected SHADOW.

The source process runs with its pinned interpreter; research implementation is
never imported into the protected runtime. No broker credentials are involved.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_v4_guardian_shadow import NATIVE, guardian_and_projection
from test_v4_timing import IDENTITY

PRODUCER = """
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from copytrade_pm.pm03_account import digest
from copytrade_pm.pm_forward_journal import PaperJournal
from copytrade_pm.pm_forward_source_stream import PersistentAccount
from copytrade_pm.v4_source_export import export_signed_events

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

identity, base, key_path = sys.argv[1:]
key = Ed25519PrivateKey.from_private_bytes(Path(key_path).read_bytes())
journal = PaperJournal(Path(base) / 'native')
account = PersistentAccount(
    journal=journal, alias='pod-a', address=identity,
    dex_catalog=[None], subaccounts=None, parent_alias='pod-a',
    scope_sha=digest(['selected', identity]),
)
account.begin(iso(100))
for known, qty in ((101, 0), (200, 20)):
    positions = [] if not qty else [{
        'position': {
            'coin': 'BTC', 'szi': str(qty),
            'positionValue': str(abs(qty) * 100),
        },
    }]
    frame = {
        'channel': 'allDexsClearinghouseState',
        'data': {
            'user': identity,
            'clearinghouseStates': [
                ['', {
                    'marginSummary': {
                        'accountValue': '1000', 'totalNtlPos': '2000',
                        'totalMarginUsed': '10',
                    },
                    'assetPositions': positions,
                    'time': known - 1,
                }],
            ],
        },
    }
    wire = json.dumps(frame, separators=(',', ':'))
    account.observe(
        frame, iso(known), raw_sha=sha256(wire.encode()).hexdigest(),
        raw_text=wire,
    )
result = export_signed_events(
    journal.path, selected={'pod-a': identity}, signer=key,
    after={'pod-a': 0},
)
print(json.dumps(result, sort_keys=True))
"""


def test_real_producer_hash_chain_to_guardian_denial(tmp_path):
    source_root = os.environ.get("PROP_RESEARCH_ROOT")
    if not source_root:
        pytest.skip("Set PROP_RESEARCH_ROOT to the verified companion checkout")
    python = Path(source_root) / ".venv/bin/python"
    if not python.is_file():
        pytest.skip("The exact pinned Python 3.14 research venv is required")
    guardian, projection, signer, broker, finance, floor = guardian_and_projection(tmp_path)
    key = tmp_path / "source.key"
    key.write_bytes(signer.private_bytes_raw())
    key.chmod(0o600)
    exported = subprocess.run(
        [str(python), "-c", PRODUCER, IDENTITY, str(tmp_path), str(key)],
        cwd=source_root,
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(source_root) / "src")},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert exported.returncode == 0, exported.stderr
    payload = json.loads(exported.stdout)
    for receipt in payload["events"]["pod-a"]:
        projection.ingest(receipt, now_ms=receipt["proof"]["payload"]["first_known_ms"])
    assert projection.due(300199) == []
    action = projection.due(300200)[0]
    assert projection.eligible_pods(300200)[0]["requested_notionals"][NATIVE] == "50.000"
    quote = {NATIVE: {"bid": "10000", "ask": "10030", "known_at_ms": 300200}}
    decision = guardian.preview_v4(projection, action["action_id"], quote, 300200)
    assert decision.data["status"] == "BLOCKED"
    assert decision.data["trading_authorized"] is False
    assert not finance.snapshot()["intents"]
    assert not broker.audit()
    projection.close()
    finance.close()
    broker.close()
    floor.close()
