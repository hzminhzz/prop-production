"""Bounded Unix-domain RPC with OS peer identity, per-role keys and fresh challenges."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence import canonical, load_json

MAX_FRAME = 1000000


def _receive(connection: socket.socket) -> dict[str, Any]:
    def exact(count: int) -> bytes:
        pieces = bytearray()
        while len(pieces) < count:
            chunk = connection.recv(count - len(pieces))
            if not chunk:
                raise ValueError("IPC_TRUNCATED")
            pieces.extend(chunk)
        return bytes(pieces)
    length = struct.unpack("!I", exact(4))[0]
    if not 0 < length <= MAX_FRAME:
        raise ValueError("IPC_FRAME_SIZE")
    result = load_json(exact(length))
    if not isinstance(result, dict):
        raise ValueError("IPC_SCHEMA")
    return dict(result)


def _send(connection: socket.socket, value: dict[str, Any]) -> None:
    body = canonical(value)
    if len(body) > MAX_FRAME:
        raise ValueError("IPC_FRAME_SIZE")
    connection.sendall(struct.pack("!I", len(body)) + body)


def _mac(key: bytes, value: dict[str, Any]) -> str:
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


def _uid(connection: socket.socket) -> int:
    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return int(uid)


class LocalClient:
    def __init__(self, path: Path, role: str, key: bytes, server_uid: int):
        self.path, self.role, self.key, self.server_uid = path, role, key, server_uid

    def call(self, body: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.path))
            if _uid(connection) != self.server_uid:
                raise PermissionError("IPC_SERVER_IDENTITY")
            hello = _receive(connection)
            request = {"schema": "local_rpc_v1", "role": self.role, "challenge": hello["challenge"],
                       "nonce": secrets.token_hex(24), "body": body}
            _send(connection, {"request": request, "mac": _mac(self.key, request)})
            reply = _receive(connection)
            if "error" in reply:
                raise PermissionError("IPC_AUTH_OR_PERMISSION")
            response = reply["response"]
            if (not hmac.compare_digest(reply["mac"], _mac(self.key, response))
                    or response["nonce"] != request["nonce"]
                    or response["challenge"] != request["challenge"]):
                raise PermissionError("IPC_REPLY_AUTH")
            if "error" in response["body"]:
                raise ValueError(response["body"]["error"])
            return dict(response["body"])


class LocalServer:
    def __init__(self, path: Path, roles: dict[str, tuple[int, bytes, set[str]]]):
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise ValueError("IPC_PATH_NOT_EXCLUSIVE")
        if path.parent.stat().st_mode & 0o002:
            raise ValueError("IPC_DIRECTORY_WRITABLE_BY_OTHERS")
        self.path, self.roles = path, roles
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        os.chmod(path, 0o600)
        self.socket.listen(8)
        self.socket.settimeout(1)

    def serve_once(self, handler: Callable[[str, dict[str, Any]], dict[str, Any]]) -> None:
        try:
            connection, _ = self.socket.accept()
        except TimeoutError:
            return
        with connection:
            connection.settimeout(3)
            challenge = secrets.token_hex(32)
            _send(connection, {"schema": "local_rpc_v1", "challenge": challenge})
            try:
                envelope = _receive(connection)
                request = envelope["request"]
                if (set(envelope) != {"request", "mac"}
                        or set(request) != {"schema", "role", "challenge", "nonce", "body"}
                        or request["schema"] != "local_rpc_v1" or request["challenge"] != challenge
                        or not isinstance(request["nonce"], str) or len(request["nonce"]) != 48):
                    raise ValueError("IPC_SCHEMA")
                uid, key, commands = self.roles[request["role"]]
                if (_uid(connection) != uid or not hmac.compare_digest(envelope["mac"], _mac(key, request))
                        or request["body"].get("command") not in commands):
                    raise PermissionError("IPC_AUTH")
            except (ValueError, KeyError, TypeError, PermissionError):
                _send(connection, {"error": "IPC_AUTH"})
                return
            try:
                body = handler(request["role"], request["body"])
            except (ValueError, KeyError, TypeError, PermissionError):
                body = {"error": "IPC_REQUEST_REJECTED"}
            response = {"challenge": challenge, "nonce": request["nonce"], "body": body}
            _send(connection, {"response": response, "mac": _mac(key, response)})

    def close(self) -> None:
        self.socket.close()
        if self.path.exists():
            self.path.unlink()
