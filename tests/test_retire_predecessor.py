"""Audited retire-predecessor receipt: the fence stays default and fails closed."""
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_release_integrity import bundle

from prop_production.deployment import (
    UNITS,
    activate_release,
    retire_predecessor,
    seal_bundle,
)

PREDECESSOR, CANDIDATE = "c" * 40, "a" * 40


def make_manifest(release: Path, *, extra_wire: int | None = None, mode: str = "SHADOW") -> None:
    manifest = json.loads((release / "compatibility.json").read_text())
    if extra_wire is not None:
        manifest["contracts"]["source_wire_versions"].append(extra_wire)
    manifest["contracts"]["execution_mode"] = mode
    (release / "compatibility.json").write_text(json.dumps(manifest))
    if mode == "SHADOW":
        seal_bundle(release)


def setup(tmp_path: Path, *, fenced: bool = True):
    opt = tmp_path / "opt"
    candidate = bundle(opt, CANDIDATE)
    previous = bundle(opt, PREDECESSOR)
    if fenced:
        make_manifest(previous, extra_wire=99)
    (opt / "current").symlink_to(previous)
    units = tmp_path / "units"
    units.mkdir()
    for unit in UNITS:
        (units / unit).write_text("old unit: " + unit)
    return opt, units, candidate, previous, tmp_path / "account.sqlite"


def activate(opt, units, candidate, journal, calls=None):
    activate_release(opt, units, candidate, previous_active=list(UNITS),
                     health_check=lambda: None, journal=journal,
                     service=(calls if calls is not None else []).append)


def fenced(opt, units, candidate, journal):
    calls: list[list[str]] = []
    with pytest.raises(ValueError, match="ACTIVATION_INCOMPATIBLE_DURABLE_CONTRACTS"):
        activate(opt, units, candidate, journal, calls)
    assert ["stop", *UNITS] in calls
    assert (opt / "code-fence.json").exists()
    assert (opt / "current").resolve().name == PREDECESSOR


def receipt_file(opt: Path) -> Path:
    return opt / "retirements" / f"{PREDECESSOR}__{CANDIDATE}.json"


def test_fence_without_receipt_is_default(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    fenced(opt, units, candidate, journal)


def test_receipt_for_absent_journal_allows_activation_and_clears_matching_fence(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    fenced(opt, units, candidate, journal)
    receipt = retire_predecessor(opt, candidate, journal=journal)
    path = receipt_file(opt)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert receipt["journal"] == {"state": "ABSENT"}
    assert receipt["trading_authorized"] is False and receipt["execution_mode"] == "SHADOW"
    assert os.readlink(opt / "current").endswith(PREDECESSOR)
    activate(opt, units, candidate, journal)
    assert (opt / "current").resolve().name == CANDIDATE
    record = json.loads((opt / "activation.json").read_text())
    assert record["phase"] == "ACTIVE"
    assert record["retired_predecessor_receipt"] == receipt["receipt_sha"]
    assert not (opt / "code-fence.json").exists()


def test_unrelated_fence_is_left_in_place(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    retire_predecessor(opt, candidate, journal=journal)
    (opt / "code-fence.json").write_text(json.dumps({"evidence_sha": "0" * 64}))
    activate(opt, units, candidate, journal)
    assert (opt / "code-fence.json").exists()


def test_receipt_rejected_when_journal_later_appears_unverifiable(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    retire_predecessor(opt, candidate, journal=journal)
    journal.write_bytes(b"not a sqlite journal")
    fenced(opt, units, candidate, journal)


def test_verified_v1_journal_is_recorded_and_rechecked(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    from checkpoint_archive_fixture import presemantic_archive

    archive = presemantic_archive(tmp_path, release_sha="e" * 64)
    archive.rename(journal)
    receipt = retire_predecessor(opt, candidate, journal=journal)
    assert receipt["journal"]["state"] == "VERIFIED_V1"
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["journal"]["head"])
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute("UPDATE events SET kind = 'TAMPERED' WHERE seq = 1")
    fenced(opt, units, candidate, journal)


def test_unverifiable_journal_blocks_retire(tmp_path):
    opt, _, candidate, _, journal = setup(tmp_path)
    journal.write_bytes(b"garbage")
    with pytest.raises(ValueError, match="RETIRE_JOURNAL_UNVERIFIABLE"):
        retire_predecessor(opt, candidate, journal=journal)
    assert not receipt_file(opt).exists()


def test_tampered_receipt_digest_rejected(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    retire_predecessor(opt, candidate, journal=journal)
    value = json.loads(receipt_file(opt).read_text())
    value["at_ms"] += 1
    receipt_file(opt).write_text(json.dumps(value))
    os.chmod(receipt_file(opt), 0o600)
    fenced(opt, units, candidate, journal)


def test_loose_receipt_permissions_rejected(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    retire_predecessor(opt, candidate, journal=journal)
    os.chmod(receipt_file(opt), 0o644)
    fenced(opt, units, candidate, journal)


def test_receipt_for_a_different_candidate_rejected(tmp_path):
    opt, units, candidate, _, journal = setup(tmp_path)
    other = bundle(opt, "d" * 40)
    retire_predecessor(opt, other, journal=journal)
    receipt_file_other = opt / "retirements" / f"{PREDECESSOR}__{'d' * 40}.json"
    receipt_file_other.rename(receipt_file(opt))
    fenced(opt, units, candidate, journal)


def test_retire_not_required_for_compatible_pair(tmp_path):
    opt, _, candidate, _, journal = setup(tmp_path, fenced=False)
    with pytest.raises(ValueError, match="RETIRE_NOT_REQUIRED"):
        retire_predecessor(opt, candidate, journal=journal)


def test_candidate_must_differ_from_predecessor(tmp_path):
    opt, _, _, previous, journal = setup(tmp_path)
    with pytest.raises(ValueError, match="RETIRE_CANDIDATE_IS_PREDECESSOR"):
        retire_predecessor(opt, previous, journal=journal)


@pytest.mark.parametrize("phase", ["PREPARED", "ROLLBACK_FAILED"])
def test_interrupted_activation_blocks_retire(tmp_path, phase):
    opt, _, candidate, _, journal = setup(tmp_path)
    (opt / "activation.json").write_text(json.dumps({"phase": phase}))
    with pytest.raises(ValueError, match="RETIRE_ACTIVATION_INTERRUPTED"):
        retire_predecessor(opt, candidate, journal=journal)


def test_non_shadow_candidate_refused(tmp_path):
    opt, _, candidate, _, journal = setup(tmp_path)
    make_manifest(candidate, mode="LIVE")
    with pytest.raises(ValueError):
        retire_predecessor(opt, candidate, journal=journal)
    assert not receipt_file(opt).exists()


def test_receipt_holds_no_raw_wallet_addresses(tmp_path):
    opt, _, candidate, _, journal = setup(tmp_path)
    retire_predecessor(opt, candidate, journal=journal)
    assert re.search(r"0x[0-9a-fA-F]{40}", receipt_file(opt).read_text()) is None
