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

RESEARCH_SHA="fcf79b8e4d8012b46cdf8d2ad190af06a6e8dd5e"
CONNECTOR_SHA="ec3f03a5d24a2db3b0c75fe61e3a3fbfa662b343"
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
        verify_clean "$PROP_RESEARCH_ROOT" "src pyproject.toml tests"
        verify_clean "$PROP_CONNECTOR_ROOT" "connectors pyproject.toml tests"
    fi
    export PROP_RESEARCH_ROOT
fi

printf 'LOCAL_VERIFY_START app=%s mode=%s\n' "$(git -C "$APP_ROOT" rev-parse HEAD)" "$MODE"
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
        .venv/bin/python -m pytest -q connectors/propr/tests
    )
    printf '\n[research / %s]\n' "$RESEARCH_SHA"
    (
        cd "$PROP_RESEARCH_ROOT"
        uv run --frozen --extra actuary --group dev pytest -q
        uv run --frozen --group dev copytrade-pm capabilities validate
    )
fi

printf 'LOCAL_VERIFY_PASS app=%s mode=%s research=%s connector=%s\n' \
    "$(git -C "$APP_ROOT" rev-parse HEAD)" "$MODE" \
    "$([[ "$MODE" == "all" ]] && printf '%s' "$RESEARCH_SHA" || printf '%s' 'NOT_TESTED')" \
    "$([[ "$MODE" == "all" ]] && printf '%s' "$CONNECTOR_SHA" || printf '%s' 'NOT_TESTED')"
printf 'Scope: ENGINEERING_TESTS_ONLY; G0-G4 and live trading remain BLOCKED\n'
