#!/usr/bin/env bash
# Credential-free verification on the private VPS; no GitHub Actions dependency.
set -euo pipefail
# The research suite deliberately tests public vs private directory modes.
# Do not impose a 077 umask on fixtures; protected logs belong outside this script.
umask 022

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-all}"
if [[ "$MODE" != "app" && "$MODE" != "all" ]]; then
    printf 'Usage: %s [app|all]\n' "$0" >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'BLOCKED: pinned uv is unavailable\n' >&2
    exit 2
fi

verify_clean() {
    local path="$1" scope="$2"
    if [[ -n "$(git -C "$path" status --porcelain=v1 --untracked-files=all -- $scope)" ]]; then
        printf 'BLOCKED: dirty verification source at %s (%s)\n' "$path" "$scope" >&2
        exit 2
    fi
}

if [[ "${GATE_CLEAN:-1}" == "1" ]]; then
    verify_clean "$APP_ROOT" "."
else
    printf 'WARNING: GATE_CLEAN=0; results are developmental, not a clean-revision acceptance\n' >&2
fi

pin_field() {
    PYTHONPATH="$APP_ROOT/src" python3 -m prop_production.compatibility --field "$1"
}
RESEARCH_SHA="$(pin_field research)"
CONNECTOR_SHA="$(pin_field connector)"
COMPATIBILITY_SHA="$(pin_field sha)"
PYTHONPATH="$APP_ROOT/src" python3 -m prop_production.compatibility \
    --check-project "$APP_ROOT/pyproject.toml" --field sha
if [[ "$MODE" == "all" ]]; then
    : "${PROP_RESEARCH_ROOT:?BLOCKED: set PROP_RESEARCH_ROOT to the pinned copytrade-pm worktree}"
    : "${PROP_CONNECTOR_ROOT:?BLOCKED: set PROP_CONNECTOR_ROOT to the pinned trading-connector worktree}"
    for item in "PROP_RESEARCH_ROOT:$RESEARCH_SHA" "PROP_CONNECTOR_ROOT:$CONNECTOR_SHA"; do
        name="${item%%:*}"
        sha="${item#*:}"
        path="${!name}"
        if [[ ! -d "$path/.git" && ! -f "$path/.git" ]]; then
            printf 'BLOCKED: no Git checkout for %s\n' "$name" >&2
            exit 2
        fi
        if [[ "$(git -C "$path" rev-parse HEAD)" != "$sha" ]]; then
            printf 'BLOCKED: %s is not at pinned commit %s\n' "$name" "$sha" >&2
            exit 2
        fi
    done
    if [[ "${GATE_CLEAN:-1}" == "1" ]]; then
        verify_clean "$PROP_RESEARCH_ROOT" "."
        verify_clean "$PROP_CONNECTOR_ROOT" "."
    fi
    export PROP_RESEARCH_ROOT PROP_CONNECTOR_ROOT
fi

APP_SHA="$(git -C "$APP_ROOT" rev-parse HEAD)"
printf 'LOCAL_VERIFY_START app=%s mode=%s\n' "$APP_SHA" "$MODE"
uv --version
cd "$APP_ROOT"
for version in 3.13 3.14; do
    printf '\n[app / Python %s]\n' "$version"
    uv sync --python "$version" --frozen --group dev
    uv run --python "$version" --frozen --group dev pytest -q
    uv run --python "$version" --frozen --group dev ruff check src tests
    uv run --python "$version" --frozen --group dev mypy
    uv run --python "$version" --frozen --group dev prop-production status
    uv run --python "$version" --frozen --group dev prop-production smoke
done
uv build --no-sources
git diff --check

if [[ "$MODE" == "all" ]]; then
    printf '\n[connector / %s]\n' "$CONNECTOR_SHA"
    (
        cd "$PROP_CONNECTOR_ROOT"
        PYTHONPATH="$PROP_RESEARCH_ROOT/src" .venv/bin/python -m pytest -q -rs connectors
        .venv/bin/python -m ruff check connectors propr_sdk.py
        uv build --offline --quiet
    )
    printf '\n[app + native connector / %s]\n' "$CONNECTOR_SHA"
    PYTHONPATH="$PROP_CONNECTOR_ROOT" uv run --python 3.13 --frozen --extra connector --group dev pytest -q tests/test_native_gateway.py
    printf '\n[Bybit Demo native adapter / %s]\n' "$CONNECTOR_SHA"
    PYTHONPATH="$PROP_CONNECTOR_ROOT" uv run --python 3.13 --frozen --extra connector --group dev pytest -q tests/test_bybit_gateway.py tests/test_bybit_observer.py
    printf '\n[research / %s]\n' "$RESEARCH_SHA"
    (
        cd "$PROP_RESEARCH_ROOT"
        uv sync --frozen --all-extras --group dev
        PYTHONPATH="$PROP_RESEARCH_ROOT" uv run --no-sync pytest -q
        uv run --no-sync ruff check src tests scripts
        uv run --no-sync ruff format --check src tests
        uv build --offline --quiet
        uv run --no-sync copytrade-pm capabilities validate
    )
fi

# A receipt is valid only for the same clean revisions that began the gate.
[[ "$(git -C "$APP_ROOT" rev-parse HEAD)" == "$APP_SHA" ]] || exit 2
if [[ "${GATE_CLEAN:-1}" == "1" ]]; then
    verify_clean "$APP_ROOT" "."
    if [[ "$MODE" == "all" ]]; then
        verify_clean "$PROP_RESEARCH_ROOT" "."
        verify_clean "$PROP_CONNECTOR_ROOT" "."
        [[ "$(git -C "$PROP_RESEARCH_ROOT" rev-parse HEAD)" == "$RESEARCH_SHA" ]] || exit 2
        [[ "$(git -C "$PROP_CONNECTOR_ROOT" rev-parse HEAD)" == "$CONNECTOR_SHA" ]] || exit 2
    fi
fi
printf 'LOCAL_VERIFY_PASS app=%s mode=%s research=%s connector=%s compatibility=%s\n' \
    "$(git -C "$APP_ROOT" rev-parse HEAD)" "$MODE" \
    "$([[ "$MODE" == "all" ]] && printf '%s' "$RESEARCH_SHA" || printf '%s' 'NOT_TESTED')" \
    "$([[ "$MODE" == "all" ]] && printf '%s' "$CONNECTOR_SHA" || printf '%s' 'NOT_TESTED')" \
    "$COMPATIBILITY_SHA"
printf 'Scope: ENGINEERING_TESTS_ONLY; G0-G4 and live trading remain BLOCKED\n'
