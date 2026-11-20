"""Enforce the kernel dependency and Guardian provenance boundaries."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "packages/copytrade-kernel/src/copytrade_kernel"


def test_kernel_uses_only_pure_stdlib_dependencies():
    allowed = {
        "__future__",
        "collections",
        "copy",
        "dataclasses",
        "decimal",
        "enum",
        "fractions",
        "re",
        "typing",
    }
    for path in KERNEL.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in allowed for alias in node.names), path
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module and node.module.split(".")[0] in allowed, path
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {
                    "open",
                    "input",
                    "print",
                    "exec",
                    "eval",
                    "__import__",
                }, path


def test_guardian_rejects_retrospective_input_in_strict_mypy(tmp_path):
    good = tmp_path / "prospective.py"
    bad = tmp_path / "retrospective.py"
    body = """from copytrade_kernel import CheckpointBatch, DecisionMs, KernelState, ProspectiveCWSA, RetrospectiveArchive
from prop_production.checkpoint_admission import prospective_targets
from prop_production.checkpoint_receiver import CheckpointSourceReceiver
from prop_production.guardian import Guardian

def check(guardian: Guardian, receiver: RECEIVER, frame: CheckpointBatch[PROVENANCE]) -> None:
    guardian.process_checkpoint(receiver, {}, quotes={}, now_ms=DecisionMs(0))
    prospective_targets(KernelState(()), frame)
"""
    good.write_text(
        body.replace("RECEIVER", "CheckpointSourceReceiver").replace(
            "PROVENANCE", "ProspectiveCWSA"
        )
    )
    bad.write_text(
        body.replace("RECEIVER", "CheckpointBatch[RetrospectiveArchive]").replace(
            "PROVENANCE", "RetrospectiveArchive"
        )
    )

    def check(path):
        return subprocess.run(
            [sys.executable, "-m", "mypy", "--strict", str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    result = check(good)
    assert result.returncode == 0, result.stdout + result.stderr
    result = check(bad)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Argument 1 to "process_checkpoint"' in result.stdout
    assert 'Argument 2 to "prospective_targets"' in result.stdout
    assert result.stdout.count("[arg-type]") == 2, result.stdout
