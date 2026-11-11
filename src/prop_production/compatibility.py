"""Authoritative companion pins; the containing Git commit binds the application."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any


def load_manifest() -> dict[str, Any]:
    value = json.loads(files("prop_production").joinpath("compatibility.json").read_text())
    if (value.get("schema") != "prop_production_compatibility_v1"
            or value.get("application_revision") != "CONTAINING_GIT_COMMIT"):
        raise ValueError("COMPATIBILITY_SCHEMA")
    for name in ("copytrade-pm", "trading-connector"):
        item = value["repositories"][name]
        if (item["repository"] != "hzminhzz/" + name
                or not re.fullmatch(r"[0-9a-f]{40}", item["commit"])):
            raise ValueError("COMPATIBILITY_REPOSITORY_PIN")
    contracts = value["contracts"]
    if (contracts["portfolio_release"] != "portfolio_release_v1"
            or contracts["propr_evidence"] != "propr_native_evidence_v1"
            or contracts["bybit_evidence"] != "bybit_demo_snapshot_v1"
            or contracts["default_sizing_policy"] != "FIXED_K_NATIVE_QUANTITY"
            or contracts["legacy_sizing_policy"] != "ALLOCATION_OVER_LIVE_NAV"
            or contracts["execution_mode"] != "SHADOW"
            or contracts.get("source_wire_versions") != [1, 2, 3]
            or contracts.get("active_source_wire") != 1
            or contracts["trading_authorized"] is not False):
        raise ValueError("COMPATIBILITY_CONTRACT")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["dependencies"]["bybit_ccxt"]):
        raise ValueError("COMPATIBILITY_DEPENDENCY")
    return dict(value)


def manifest_sha() -> str:
    return hashlib.sha256(
        files("prop_production").joinpath("compatibility.json").read_bytes()
    ).hexdigest()


MANIFEST = load_manifest()
RESEARCH_COMMIT: str = MANIFEST["repositories"]["copytrade-pm"]["commit"]
CONNECTOR_COMMIT: str = MANIFEST["repositories"]["trading-connector"]["commit"]
BYBIT_CCXT: str = MANIFEST["dependencies"]["bybit_ccxt"]


def check_dependency_constraints(project: Path) -> None:
    declared = tomllib.loads(project.read_text())["project"]["optional-dependencies"]["connector"]
    expected = {
        "trading-connector-library @ git+https://github.com/hzminhzz/"
        f"trading-connector.git@{CONNECTOR_COMMIT}",
        f"ccxt=={BYBIT_CCXT}",
    }
    if not expected.issubset(set(declared)):
        raise ValueError("COMPATIBILITY_DEPENDENCY_DRIFT")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", choices=("research", "connector", "ccxt", "sha"))
    parser.add_argument("--check-project", type=Path)
    args = parser.parse_args()
    if args.check_project:
        check_dependency_constraints(args.check_project)
    fields = {"research": RESEARCH_COMMIT, "connector": CONNECTOR_COMMIT,
              "ccxt": BYBIT_CCXT, "sha": manifest_sha()}
    print(fields[args.field] if args.field else json.dumps(MANIFEST, sort_keys=True))


if __name__ == "__main__":
    main()
