"""Credential-free CLI contract; independent of any provider or private dataset."""
import importlib
import json
import socket
import subprocess
import sys


def test_status_is_disarmed_without_network_or_credentials(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("startup attempted network or credential access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    cli = importlib.import_module("prop_production.cli")
    assert cli.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "SHADOW"
    assert status["state"] == "DISARMED"
    assert status["account_readiness"] == "UNKNOWN"
    assert status["order_route"] == "NONE"
    assert status["trading_authorized"] is False
    assert not any(name.startswith("connectors") for name in sys.modules)


def test_invalid_mode_has_stable_nonzero_exit(capsys):
    from prop_production.cli import main

    assert main(["--mode", "LIVE", "status"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "MODE_NOT_PERMITTED"


def test_clean_process_smoke_is_repeatable():
    commands = [sys.executable, "-m", "prop_production", "smoke"]
    first = subprocess.run(commands, capture_output=True, text=True, check=True)
    second = subprocess.run(commands, capture_output=True, text=True, check=True)
    assert first.stdout == second.stdout
    result = json.loads(first.stdout)
    assert result["synthetic"] is True
    assert result["equity"] == "10000"
    assert result["order_route"] == "NONE"
