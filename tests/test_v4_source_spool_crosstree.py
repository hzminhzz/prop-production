"""Real Copytrade 3.14 source journals -> protected 3.13 root-style transfer.

Everything runs in isolated temporary directories with invented test identities,
test-only signatures and zero broker credentials. Native source signatures come
from the real Copytrade producer, not a handcrafted JSON proof.
"""

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from test_v4_timing import IDENTITY, setup

from prop_production.evidence import canonical
from prop_production.v4_source_handoff import transfer_once
from prop_production.v4_source_receiver import SourcePacketReceiver, consume_once

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

identity, root, key_path = sys.argv[1:]
root = Path(root)
key = Ed25519PrivateKey.from_private_bytes(Path(key_path).read_bytes())
journal = PaperJournal(root / 'native')
account = PersistentAccount(
    journal=journal, alias='pod-a', address=identity,
    dex_catalog=[None], subaccounts=None, parent_alias='pod-a',
    scope_sha=digest(['selected', identity]),
)
account.begin(iso(100))

def frame(ts, qty):
    positions = [] if qty == 0 else [{'position': {
        'coin': 'BTC', 'szi': str(qty), 'positionValue': str(abs(qty) * 100),
    }}]
    return {
        'channel': 'allDexsClearinghouseState',
        'data': {'user': identity, 'clearinghouseStates': [
            ['', {
                'marginSummary': {
                    'accountValue': '1000', 'totalNtlPos': '2000',
                    'totalMarginUsed': '10',
                },
                'assetPositions': positions, 'time': ts - 1,
            }]
        ]},
    }

def observe(ts, qty):
    payload = frame(ts, qty)
    wire = json.dumps(payload, separators=(',', ':'))
    account.observe(
        payload, iso(ts), raw_sha=sha256(wire.encode()).hexdigest(),
        raw_text=wire,
    )

observe(101, 0)
baseline = export_signed_events(
    journal.path, selected={'pod-a': identity}, signer=key,
)
assert baseline['events']['pod-a'] == []
head = baseline['head_cursors']['pod-a']
baseline_v2 = export_signed_events(
    journal.path, selected={'pod-a': identity}, signer=key,
    wire_version=2, wire_epoch='d' * 32, cutoff_ms=150,
)
observe(200, 20)
followup_v2 = export_signed_events(
    journal.path, selected={'pod-a': identity}, signer=key,
    wire_version=2, wire_epoch='d' * 32,
    anchor=baseline_v2['anchor'], after={'pod-a': head},
)
followup = export_signed_events(
    journal.path, selected={'pod-a': identity}, signer=key,
    after={'pod-a': head},
)
print(json.dumps({'baseline': baseline, 'followup': followup,
    'baseline_v2': baseline_v2, 'followup_v2': followup_v2}, sort_keys=True))
"""


def write_private(directory: Path, body: dict, sequence: int) -> Path:
    raw = canonical(body)
    path = directory / f"{sequence:020d}-{sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_full_sha_source_spool_transfer_receiver_and_t300(tmp_path):
    source_root = os.environ.get("PROP_RESEARCH_ROOT")
    if not source_root:
        pytest.skip("Requires pinned Copytrade producer worktree")
    py = Path(source_root) / ".venv/bin/python"
    if not py.is_file():
        pytest.skip("Requires Copytrade Python 3.14 environment")

    original, release, key = setup(tmp_path)
    original.close()
    key_path = tmp_path / "test-only-source.key"
    key_path.write_bytes(key.private_bytes_raw())
    key_path.chmod(0o600)

    produced = subprocess.run(
        [str(py), "-c", PRODUCER, IDENTITY, str(tmp_path), str(key_path)],
        cwd=source_root,
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(source_root) / "src")},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr
    reports = json.loads(produced.stdout)
    src = tmp_path / "quant-outbox"
    dst = tmp_path / "protected-inbox"
    src.mkdir(mode=0o700)
    dst.mkdir(mode=0o700)
    write_private(src, reports["baseline"], 1)

    def copy() -> dict:
        return transfer_once(
            source_outbox=src,
            protected_inbox=dst,
            source_key=key.public_key(),
            source_uid=os.getuid(),
            destination_uid=os.getuid(),
            destination_gid=os.getgid(),
            enforce_root=False,
        )

    assert copy()["transferred"] == 1
    db = tmp_path / "receiver.sqlite"
    baseline = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=150,
    )
    assert baseline["processed_source_events"] == 0
    assert baseline["trading_authorized"] is False

    write_private(src, reports["followup"], 2)
    assert copy()["transferred"] == 1
    result = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=200,
    )
    assert result["processed_source_events"] == 1
    assert result["order_route"] == "NONE"
    assert result["guardian_financial_admission"] == "BLOCKED_NOT_WIRED"

    receiver = SourcePacketReceiver(
        db,
        release=release,
        source_key=key.public_key(),
    )
    assert receiver.due(now_ms=300199) == []
    actions = receiver.due(now_ms=300200)
    assert len(actions) == 1
    assert actions[0]["kind"] == "ENTRY_ELIGIBLE"
    assert actions[0]["first_known_ms"] == 200
    assert actions[0]["due_ms"] == 300200
    assert actions[0]["eligible_source_qty"] == "20"
    receiver.close()

    replay = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=300201,
    )
    assert replay["processed_source_events"] == 0
    assert replay["due_shadow_eligibility"] == 0


def test_real_source_signed_v2_anchor_spool_receiver_and_restart(tmp_path):
    source_root = os.environ.get("PROP_RESEARCH_ROOT")
    if not source_root:
        pytest.skip("Requires exact verified Copytrade producer worktree")
    py = Path(source_root) / ".venv/bin/python"
    if not py.is_file():
        pytest.skip("Requires Copytrade Python 3.14 environment")
    original, release, key = setup(tmp_path)
    original.close()
    key_path = tmp_path / "v2-synthetic-only-source.key"
    key_path.write_bytes(key.private_bytes_raw())
    key_path.chmod(0o600)
    produced = subprocess.run(
        [str(py), "-c", PRODUCER, IDENTITY, str(tmp_path), str(key_path)],
        cwd=source_root,
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(source_root) / "src")},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr
    reports = json.loads(produced.stdout)
    src = tmp_path / "quant-source-v2"
    dst = tmp_path / "protected-v2"
    src.mkdir(mode=0o700)
    dst.mkdir(mode=0o700)
    db = tmp_path / "v2-protected.sqlite"
    write_private(src, reports["baseline_v2"], 1)

    def transfer():
        return transfer_once(
            source_outbox=src,
            protected_inbox=dst,
            source_key=key.public_key(),
            source_uid=os.getuid(),
            destination_uid=os.getuid(),
            destination_gid=os.getgid(),
            enforce_root=False,
        )

    assert transfer()["transferred"] == 1
    baseline = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=150,
        wire_version=2,
    )
    assert baseline["processed_source_events"] == 0
    write_private(src, reports["followup_v2"], 2)
    assert transfer()["transferred"] == 1
    followup = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=200,
        wire_version=2,
    )
    assert followup["processed_source_events"] == 1
    assert followup["order_route"] == "NONE"
    receiver = SourcePacketReceiver(
        db,
        release=release,
        source_key=key.public_key(),
        wire_version=2,
    )
    assert receiver.due(now_ms=300199) == []
    due = receiver.due(now_ms=300200)
    assert len(due) == 1
    assert due[0]["kind"] == "ENTRY_ELIGIBLE"
    assert due[0]["first_known_ms"] == 200
    receiver.close()
    replay = consume_once(
        state_db=db,
        inbox=dst,
        release=release,
        source_key=key.public_key(),
        now_ms=300201,
        wire_version=2,
    )
    assert replay["processed_source_events"] == 0
    assert replay["due_shadow_eligibility"] == 0
    with pytest.raises(ValueError, match="DOWNGRADE|WIRE"):
        SourcePacketReceiver(db, release=release, source_key=key.public_key())
