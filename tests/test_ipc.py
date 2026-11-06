import multiprocessing
import os
import time

import pytest


def serve(path, key):
    from prop_production.ipc import LocalServer
    server = LocalServer(path, {"planner": (os.getuid(), key, {"status"})})
    for _ in range(3):
        server.serve_once(lambda role, body: {"ok": True, "role": role})
    server.close()


def test_peer_bound_authenticated_ipc_rejects_wrong_key_and_role_escalation(tmp_path):
    from prop_production.ipc import LocalClient
    path = tmp_path / "guardian.sock"
    key = b"synthetic-test-auth-key"
    child = multiprocessing.get_context("fork").Process(target=serve, args=(path, key))
    child.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.01)
    client = LocalClient(path, "planner", key, os.getuid())
    assert client.call({"command": "status"}) == {"ok": True, "role": "planner"}
    with pytest.raises(PermissionError):
        client.call({"command": "dispatch"})
    with pytest.raises(PermissionError):
        LocalClient(path, "planner", b"wrong-key", os.getuid()).call({"command": "status"})
    child.join(5)
    assert child.exitcode == 0
