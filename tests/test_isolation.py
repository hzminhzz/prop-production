import json
import subprocess
import sys


def test_linux_planner_sandbox_denies_guardian_secrets_and_network(tmp_path):
    planner = tmp_path / "planner"
    planner.mkdir(mode=0o700)
    secret = tmp_path / "guardian-secret"
    secret.write_text("never-print-this")
    program = """
import json, pathlib, socket, sys
from prop_production.isolation import restrict_planner
restrict_planner([pathlib.Path(sys.argv[1])], [])
result = {}
try:
    pathlib.Path(sys.argv[2]).read_text()
    result["secret"] = "READ"
except PermissionError:
    result["secret"] = "DENIED"
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result["network"] = "OPEN"
except PermissionError:
    result["network"] = "DENIED"
pathlib.Path(sys.argv[1], "own-state").write_text("ok")
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-c", program, str(planner), str(secret)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"secret": "DENIED", "network": "DENIED"}
    assert (planner / "own-state").read_text() == "ok"
    assert "never-print-this" not in result.stdout + result.stderr
