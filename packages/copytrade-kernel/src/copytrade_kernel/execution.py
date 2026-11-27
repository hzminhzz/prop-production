"""Net account delta, pair-local fences, and reduce-only projection."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal

from .money import add, decimal


def _is_sub_lot(quantity: Decimal, spec: dict[str, Any], quote: dict[str, Any]) -> bool:
    executable = decimal(quote["ask"] if quantity > 0 else quote["bid"])
    return abs(quantity) < decimal(spec["min_qty"]) or abs(quantity * executable) < decimal(
        spec["min_notional"]
    )


@dataclass(frozen=True)
class ExecutionProjection:
    schema: Literal["checkpoint_execution_projection_v1", "checkpoint_execution_projection_v2"]
    mode: Literal["NET", "REDUCE_ONLY_FALLBACK"]
    opening_status: Literal["ELIGIBLE", "MISSED"]
    targets: dict[str, str]
    pod_targets: dict[str, dict[str, str]]
    reduce_only: dict[str, bool]
    missed_owners: tuple[str, ...] = ()

    @property
    def data(self) -> dict[str, Any]:
        result = asdict(self)
        if self.schema == "checkpoint_execution_projection_v1":
            # Historical journal contents must replay byte-for-byte.
            result.pop("missed_owners")
        else:
            result["missed_owners"] = list(self.missed_owners)
        return result


def opening_missed(plan: dict[str, Any], owner: str) -> bool:
    """Interpret one opportunity against the projection generation that created it."""
    if plan["schema"] == "checkpoint_execution_projection_v1":
        return bool(plan["opening_status"] == "MISSED")
    if plan["schema"] == "checkpoint_execution_projection_v2":
        missed = plan["missed_owners"]
        if (
            not isinstance(missed, list)
            or missed != sorted(set(missed))
            or (plan["opening_status"] == "MISSED") != bool(missed)
        ):
            raise ValueError("CHECKPOINT_OPENING_DISPOSITION_PROOF")
        return owner in missed
    raise ValueError("CHECKPOINT_EXECUTION_PROJECTION_VERSION")


def project(
    state: dict[str, Any],
    risk: dict[str, Any],
    proof: dict[str, Any],
    markets: dict[str, Any],
    quotes: dict[str, Any],
    frozen: dict[str, dict[str, str]],
    *,
    permits: Callable[[str, str], bool],
    version: Literal[
        "checkpoint_execution_projection_v1", "checkpoint_execution_projection_v2"
    ] = "checkpoint_execution_projection_v2",
    deadband_sub_lot: bool = False,
) -> dict[str, Any]:
    if version not in {
        "checkpoint_execution_projection_v1",
        "checkpoint_execution_projection_v2",
    }:
        raise ValueError("CHECKPOINT_EXECUTION_PROJECTION_VERSION")
    books = deepcopy(risk["pod_targets"])
    all_markets = sorted(set(risk["targets"]) | set(state["positions"]))
    fallback_markets: set[str] = set()
    for market in all_markets:
        old = decimal(state["positions"].get(market, "0"))
        target = decimal(risk["targets"].get(market, "0"))
        if old * target < 0 and not permits(market, "cross_zero"):
            fallback_markets.add(market)
        for owner, residual in frozen.items():
            held = decimal(residual.get(market, "0"))
            if any(
                held * decimal(book.get(market, "0")) < 0
                for pod, book in books.items()
                if pod != owner and pod not in frozen
            ):
                fallback_markets.add(market)

    # Preserve the exact v1 projection for immutable historical financial replay.
    # Only v2 applies a failure to its actual instrument instead of every market.
    fenced_markets = (
        set(all_markets)
        if fallback_markets and version == "checkpoint_execution_projection_v1"
        else fallback_markets
    )
    for owner, book in books.items():
        if owner in frozen:
            continue
        current = state["pod_positions"].get(owner, {})
        for market in sorted(fenced_markets & (set(book) | set(current))):
            old = decimal(current.get(market, "0"))
            desired = decimal(book.get(market, "0"))
            book[market] = str(
                min(abs(old), abs(desired)) * (1 if old > 0 else -1)
                if old * desired > 0
                else decimal("0")
            )
    missed_owners = tuple(
        sorted(
            owner
            for owner, original in risk["pod_targets"].items()
            if owner not in frozen
            and any(
                decimal(original.get(market, "0")) != decimal(books[owner].get(market, "0"))
                for market in fallback_markets
            )
        )
    )
    targets = {
        market: str(add(*(decimal(book.get(market, "0")) for book in books.values())))
        for market in all_markets
    }
    if deadband_sub_lot:
        for market in all_markets:
            if market in fenced_markets:
                continue
            old = decimal(state["positions"].get(market, "0"))
            target = decimal(targets[market])
            quantity = add(target, -old)
            if not quantity or target == 0:
                continue
            spec = markets[market]
            step = decimal(spec["qty_step"])
            if quantity % step:
                continue
            if _is_sub_lot(quantity, spec, quotes[market]):
                for owner in sorted(set(books) | set(state["pod_positions"])):
                    if owner in frozen:
                        continue
                    prev = state["pod_positions"].get(owner, {}).get(market)
                    if prev is None or decimal(prev) == 0:
                        if owner in books:
                            books[owner].pop(market, None)
                    else:
                        books.setdefault(owner, {})[market] = prev
                targets[market] = str(
                    add(*(decimal(book.get(market, "0")) for book in books.values()))
                )
    reducing = {}
    for market in all_markets:
        old, target = decimal(state["positions"].get(market, "0")), decimal(targets[market])
        quantity = add(target, -old)
        reduce_only = bool(old and old * quantity < 0 and abs(quantity) <= abs(old))
        reducing[market] = reduce_only
        if not quantity:
            continue
        if market in fenced_markets and not reduce_only:
            raise ValueError("NO_PROVABLE_ACCOUNT_REDUCTION")
        if reduce_only and not permits(market, "reduce_only"):
            raise ValueError("REDUCE_ONLY_CAPABILITY_UNQUALIFIED")
        if old:
            basis = state.get("checkpoint_basis", {}).get(market)
            observed = proof.get("position_basis", {}).get(market)
            if (
                basis is None
                or observed != basis
                or decimal(basis["quantity"]) != old
                or decimal(basis["entry_price"]) <= 0
            ):
                raise ValueError("INDEPENDENT_ENTRY_BASIS_UNQUALIFIED")
        spec = markets[market]
        step = decimal(spec["qty_step"])
        if quantity % step or _is_sub_lot(quantity, spec, quotes[market]):
            raise ValueError("NATIVE_ADJUSTMENT_LOT")
    return ExecutionProjection(
        version,
        "REDUCE_ONLY_FALLBACK" if fallback_markets else "NET",
        "MISSED"
        if (fallback_markets if version == "checkpoint_execution_projection_v1" else missed_owners)
        else "ELIGIBLE",
        targets,
        books,
        reducing,
        missed_owners,
    ).data
