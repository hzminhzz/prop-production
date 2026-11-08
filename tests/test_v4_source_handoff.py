"""Root-mediated signed packet transfer never overwrites or widens private scope."""

import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_source_receiver import packet, private_packet

from prop_production.v4_source_handoff import transfer_once


def directories(tmp_path):
    src = tmp_path / "quant"
    dst = tmp_path / "protected"
    src.mkdir(mode=0o700)
    dst.mkdir(mode=0o700)
    return src, dst


def copy_once(source_dir, target_dir, key):
    return transfer_once(
        source_outbox=source_dir,
        protected_inbox=target_dir,
        source_key=key.public_key(),
        source_uid=os.getuid(),
        destination_uid=os.getuid(),
        destination_gid=os.getgid(),
        enforce_root=False,
    )


def test_exact_signed_bytes_transfer_only_and_idempotent(tmp_path):
    src, dst = directories(tmp_path)
    key = Ed25519PrivateKey.generate()
    source = private_packet(src, packet(key, 1), 1)
    first = copy_once(src, dst, key)
    assert first["transferred"] == 1
    assert first["order_route"] == "NONE"
    target = dst / source.name
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    again = copy_once(src, dst, key)
    assert again["transferred"] == 0 and again["already_installed"] == 1


def test_modified_packet_wrong_key_and_symlink_are_denied(tmp_path):
    src, dst = directories(tmp_path)
    key = Ed25519PrivateKey.generate()
    source = private_packet(src, packet(key, 1), 1)
    with pytest.raises(ValueError):
        copy_once(src, dst, Ed25519PrivateKey.generate())
    source.write_bytes(b'{"tampered":true}')
    with pytest.raises(ValueError, match="DIGEST"):
        copy_once(src, dst, key)
    source.unlink()
    other = private_packet(src, packet(key, 1), 1)
    other.unlink()
    other.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="SOURCE_INTEGRITY"):
        copy_once(src, dst, key)


def test_destination_tamper_cannot_be_overwritten(tmp_path):
    src, dst = directories(tmp_path)
    key = Ed25519PrivateKey.generate()
    source = private_packet(src, packet(key, 1), 1)
    private_packet(dst, packet(key, 1), 1)
    target = dst / source.name
    target.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="COLLISION"):
        copy_once(src, dst, key)


def test_nonroot_rejected_in_production_entrypoint(tmp_path):
    src, dst = directories(tmp_path)
    key = Ed25519PrivateKey.generate()
    if os.geteuid() != 0:
        with pytest.raises(PermissionError, match="ROOT"):
            transfer_once(
                source_outbox=src,
                protected_inbox=dst,
                source_key=key.public_key(),
                source_uid=os.getuid(),
                destination_uid=os.getuid(),
                destination_gid=os.getgid(),
            )
