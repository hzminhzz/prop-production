"""Release-integrity regression tests for the installer's manifest validation."""
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "deploy" / "install.sh"


def _release_complete(release_dir: Path) -> bool:
    """Run the installer's release_complete check against a fixture directory."""
    script = f'source <(sed -n "/^release_complete()/,/^}}/p" "{INSTALLER}"); release_complete "{release_dir}"'
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0


def _make_complete_release(root: Path) -> Path:
    release = root / "releases" / "testsha"
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (release / "venv" / "bin" / "python").chmod(0o755)
    (release / "dist").mkdir()
    (release / "COMMIT").write_text("testsha\n")
    (release / "SOURCE.sha256").write_text("abc  -\n")
    wheel = release / "dist" / "prop_production-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    sums = release / "dist" / "SHA256SUMS"
    import hashlib
    sums.write_text(f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  "
                    f"{wheel.name}\n")
    return release


def test_complete_release_accepted(tmp_path):
    release = _make_complete_release(tmp_path)
    assert _release_complete(release) is True


def test_partial_release_rejected(tmp_path):
    release = _make_complete_release(tmp_path)
    (release / "venv" / "bin" / "python").unlink()  # missing interpreter
    assert _release_complete(release) is False
    (release / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (release / "venv" / "bin" / "python").chmod(0o755)
    (release / "COMMIT").unlink()  # missing marker
    assert _release_complete(release) is False


def test_tampered_wheel_rejected(tmp_path):
    release = _make_complete_release(tmp_path)
    (release / "dist" / "prop_production-0.1.0-py3-none-any.whl").write_bytes(b"tampered")
    assert _release_complete(release) is False


def test_missing_sums_rejected(tmp_path):
    release = _make_complete_release(tmp_path)
    (release / "dist" / "SHA256SUMS").unlink()
    assert _release_complete(release) is False
