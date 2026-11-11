"""Explicit code recovery transitions; durable contracts only move forward."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .evidence import digest


@dataclass(frozen=True)
class CodeRecoveryTransition:
    predecessor: Literal["CODE_BOUND"]
    event: Literal["SELECT_CODE"]
    evidence_sha: str
    at_ms: int
    guard: Literal["CAN_READ_RETAINED_CONTRACTS", "MISSING_DURABLE_CONTRACT"]
    successor: Literal["COMPATIBLE", "FENCED"]
    effect: Literal["START_DECLARED_SERVICES", "PRESERVE_STOP"]
    required: tuple[str, ...]
    supported: tuple[str, ...]


def durable_contracts(manifest: dict[str, Any]) -> tuple[str, ...]:
    c = manifest["contracts"]
    if c["execution_mode"] != "SHADOW" or c["trading_authorized"] is not False:
        raise ValueError("CODE_RECOVERY_NOT_SHADOW")
    versions = c.get("portfolio_release_versions", [c["portfolio_release"]])
    if (
        not isinstance(versions, list)
        or not versions
        or any(type(v) is not str for v in versions)
        or any(type(v) is not int for v in c["source_wire_versions"])
    ):
        raise ValueError("CODE_RECOVERY_CONTRACTS")
    result = {"release:" + v for v in versions}
    result.update("source:" + str(v) for v in c["source_wire_versions"])
    for key in ("checkpoint_source", "copy_mandate"):
        if key in c:
            if type(c[key]) is not str:
                raise ValueError("CODE_RECOVERY_CONTRACTS")
            result.add(key + ":" + c[key])
    return tuple(sorted(result))


def code_recovery(
    required: dict[str, Any], supported: dict[str, Any], *, at_ms: int
) -> CodeRecoveryTransition:
    before, after = durable_contracts(required), durable_contracts(supported)
    compatible = set(before) <= set(after)
    return CodeRecoveryTransition(
        "CODE_BOUND",
        "SELECT_CODE",
        digest({"required": required, "supported": supported}),
        at_ms,
        "CAN_READ_RETAINED_CONTRACTS" if compatible else "MISSING_DURABLE_CONTRACT",
        "COMPATIBLE" if compatible else "FENCED",
        "START_DECLARED_SERVICES" if compatible else "PRESERVE_STOP",
        before,
        after,
    )
