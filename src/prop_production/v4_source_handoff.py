"""Root-mediated, immutable V4 source-file handoff; zero financial authority.

The quant-side spool is 0700 / 0600, and never made group accessible.
Root validates the externally pinned Ed25519 source signer, packet digest,
regular-file ownership and byte caps, then publishes a new 0600 recipient-
owned file using an exclusive link in the protected inbox. Source keys,
broker keys, journals and Guardian services are never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import load_json, verify
from .v4_source_receiver import MAX_PACKET_BYTES, PACKET_NAME, _validated_batch


def _private_dir(path: Path, owner: int) -> None:
    if (
        path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != owner
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("V4_TRANSFER_PRIVATE_DIRECTORY_OR_OWNER")


def _owned_source(path: Path, uid: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
        or info.st_size > MAX_PACKET_BYTES
    ):
        raise ValueError("V4_TRANSFER_SOURCE_INTEGRITY")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        verified = os.fstat(fd)
        if (
            not stat.S_ISREG(verified.st_mode)
            or verified.st_uid != uid
            or verified.st_nlink != 1
            or verified.st_size > MAX_PACKET_BYTES
        ):
            raise ValueError("V4_TRANSFER_SOURCE_RACE")
        data = os.read(fd, MAX_PACKET_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size:
        raise ValueError("V4_TRANSFER_PARTIAL_OR_CHANGED_SOURCE")
    return data


def transfer_once(
    *,
    source_outbox: Path,
    protected_inbox: Path,
    source_key: Ed25519PublicKey,
    source_uid: int,
    destination_uid: int,
    destination_gid: int,
    enforce_root: bool = True,
) -> dict[str, object]:
    if enforce_root and os.geteuid() != 0:
        raise PermissionError("V4_TRANSFER_REQUIRES_ROOT")
    _private_dir(source_outbox, source_uid)
    _private_dir(protected_inbox, destination_uid)
    if enforce_root and source_uid == destination_uid:
        raise ValueError("V4_TRANSFER_IDENTITY_NOT_ISOLATED")

    transferred, unchanged = 0, 0
    for source in sorted(source_outbox.iterdir()):
        if PACKET_NAME.fullmatch(source.name) is None:
            raise ValueError("V4_TRANSFER_UNEXPECTED_FILE")
        raw = _owned_source(source, source_uid)
        if sha256(raw).hexdigest()[:16] != source.name[-21:-5]:
            raise ValueError("V4_TRANSFER_DIGEST_MISMATCH")
        packet = load_json(raw)
        if packet.get("authority") == "cwsa-checkpoint-v3":
            from .checkpoint_source import validate_pins, verify_transition

            proof = verify(packet, source_key, "cwsa-checkpoint-v3")
            if (
                proof["schema"] != "cwsa_checkpoint_packet_v3"
                or proof["trading_authorized"] is not False
            ):
                raise ValueError("CWSA_HANDOFF_SCHEMA")
            validate_pins(
                {"policy": proof["policy"], "scopes": {proof["scope"]["source_id"]: proof["scope"]}}
            )
            for raw_sha, original in proof["originals"].items():
                if sha256(original.encode()).hexdigest() != raw_sha:
                    raise ValueError("CWSA_HANDOFF_ORIGINAL_HASH")
            for event in proof["transitions"]:
                verify_transition(
                    proof["scope"],
                    proof["policy"],
                    event,
                    proof["originals"],
                    issued_ms=proof["issued_ms"],
                )
        else:
            proof = verify(packet["batch_envelope"], source_key, "source-observer-v4-batch")
            _validated_batch(raw, source_key, set(proof["selected"].values()))
        destination = protected_inbox / source.name
        if destination.exists() or destination.is_symlink():
            if _owned_source(destination, destination_uid) != raw:
                raise ValueError("V4_TRANSFER_DESTINATION_COLLISION")
            unchanged += 1
            continue

        staging = protected_inbox / (source.name + ".partial")
        fd = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchown(fd, destination_uid, destination_gid)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # No replace: another service can never silently overwrite
            # already installed signed source bytes.
            os.link(staging, destination, follow_symlinks=False)
            staging.unlink()
            directory = os.open(protected_inbox, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            transferred += 1
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    return {
        "schema": "v4_root_source_handoff_v1",
        "transferred": transferred,
        "already_installed": unchanged,
        "order_route": "NONE",
        "trading_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-outbox", type=Path, required=True)
    parser.add_argument("--protected-inbox", type=Path, required=True)
    parser.add_argument("--source-public", type=Path, required=True)
    parser.add_argument("--source-user", default="quant")
    parser.add_argument("--recipient-user", default="prop-runtime")
    args = parser.parse_args(argv)
    try:
        # Production CLI has no test/no-root bypass.
        public = args.source_public
        if (
            public.is_symlink()
            or not public.is_file()
            or public.stat().st_uid != 0
            or public.stat().st_mode & 0o077
        ):
            raise ValueError("V4_TRUST_KEY_NOT_ROOT_PINNED")
        source = Ed25519PublicKey.from_public_bytes(public.read_bytes())
        source_uid = pwd.getpwnam(args.source_user).pw_uid
        recipient = pwd.getpwnam(args.recipient_user)
        status = transfer_once(
            source_outbox=args.source_outbox,
            protected_inbox=args.protected_inbox,
            source_key=source,
            source_uid=source_uid,
            destination_uid=recipient.pw_uid,
            destination_gid=recipient.pw_gid,
        )
        print(json.dumps(status, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, PermissionError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "schema": "v4_root_source_handoff_v1",
                    "state": "BLOCKED",
                    "reason": type(exc).__name__,
                    "order_route": "NONE",
                    "trading_authorized": False,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
