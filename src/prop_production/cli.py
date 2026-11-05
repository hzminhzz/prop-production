"""Credential-free operator entrypoint. Live execution is deliberately unavailable."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from . import __version__


def status() -> dict[str, Any]:
    return {
        "schema": "operator_status_v1",
        "version": __version__,
        "mode": "SHADOW",
        "state": "DISARMED",
        "account_readiness": "UNKNOWN",
        "order_route": "NONE",
        "trading_authorized": False,
        "scientifically_qualified": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prop-production")
    parser.add_argument("--mode", default="SHADOW")
    parser.add_argument("command", choices=["status", "smoke"])
    args = parser.parse_args(argv)
    if args.mode != "SHADOW":
        print(json.dumps({"error": "MODE_NOT_PERMITTED", "trading_authorized": False}))
        return 2
    result = status()
    if args.command == "smoke":
        result.update({"synthetic": True, "equity": "10000", "source": "FIXTURE"})
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
