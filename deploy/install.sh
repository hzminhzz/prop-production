#!/usr/bin/env bash
# Stage an exact approved SHADOW bundle, then activate it with complete code rollback.
# Run as root; credentials flow file-to-file and never enter argv or logs.
set -euo pipefail
umask 022

REPO=${1:?Usage: install.sh CLEAN_APPROVED_APP_CHECKOUT}
CONNECTOR_SRC=${PROP_CONNECTOR_ROOT:?Set PROP_CONNECTOR_ROOT to the pinned clean checkout}
STATE=/var/lib/prop-production
OPT=/opt/prop-production
ROOT_PYTHON=${ROOT_PYTHON:-/usr/bin/python3}
UV=${UV:-/home/quant/.local/bin/uv}
[ "$(id -u)" = 0 ] || { echo "REFUSING: root release authority required"; exit 1; }
cd "$REPO"
"$ROOT_PYTHON" -c 'import os, pathlib, sys; p=pathlib.Path(sys.executable).resolve(); assert sys.version_info >= (3,13); assert p.stat().st_uid == 0 and not p.stat().st_mode & 0o022'

# Reject stale, dirty or untracked source before any OS/service changes.
DEFAULT_BRANCH=feat/account-guardian-program
git fetch origin "$DEFAULT_BRANCH"
RELEASE_SHA=$(git rev-parse "origin/$DEFAULT_BRANCH")
[ "$(git rev-parse HEAD)" = "$RELEASE_SHA" ] || { echo "REFUSING: app HEAD is not approved default"; exit 1; }
[ -z "$(git status --porcelain=v1 --untracked-files=all)" ] || { echo "REFUSING: dirty app source"; exit 1; }
pin_field() { PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.compatibility --field "$1"; }
EXPECTED_CONNECTOR_SHA=$(pin_field connector)
PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.compatibility --check-project "$REPO/pyproject.toml" --field sha
[ "$(git -C "$CONNECTOR_SRC" rev-parse HEAD)" = "$EXPECTED_CONNECTOR_SHA" ] || { echo "REFUSING: connector revision mismatch"; exit 1; }
[ -z "$(git -C "$CONNECTOR_SRC" status --porcelain=v1 --untracked-files=all)" ] || { echo "REFUSING: dirty connector source"; exit 1; }

BYBIT_SOURCE=/home/quant/.config/trading-connector/bybit-demo.env
BYBIT_SOURCE_OK=/etc/prop-production/bybit.env
BYBIT_PREFLIGHT="$BYBIT_SOURCE"
[ ! -f "$BYBIT_SOURCE_OK" ] || BYBIT_PREFLIGHT="$BYBIT_SOURCE_OK"
[ -f "$BYBIT_PREFLIGHT" ] || { echo "REFUSING: missing Bybit Demo credentials"; exit 1; }
grep -qx 'BYBIT_ENVIRONMENT=demo' "$BYBIT_PREFLIGHT"
grep -qx 'BYBIT_MODE=read_only' "$BYBIT_PREFLIGHT"
for name in BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_DEMO_EXPECTED_UID; do
    grep -q "^$name=." "$BYBIT_PREFLIGHT" || { echo "REFUSING: missing Demo identity field"; exit 1; }
done
# Legacy WAL trust floors require a separate reviewed migration, never a raw copy
# or a checkpoint inside a transaction. Ordinary upgrades do not change the floor.
if [ -f "$STATE/trust/floor.sqlite" ]; then
    "$ROOT_PYTHON" -c 'import pathlib,sqlite3,sys; c=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; assert c.execute("PRAGMA journal_mode").fetchone()[0]=="delete", "REVIEWED_OFFLINE_TRUST_MIGRATION_REQUIRED"; c.close()' "$STATE/trust/floor.sqlite"
fi

mkdir -p "$OPT/releases"
exec 9>"$OPT/.deployment.lock"
flock -n 9 || { echo "REFUSING: another deployment owns the lock"; exit 1; }
RELEASE_DIR="$OPT/releases/$RELEASE_SHA"
release_complete() {
    PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.deployment verify "$1" --commit "$RELEASE_SHA"
}
if [ -e "$RELEASE_DIR" ]; then
    release_complete "$RELEASE_DIR" || {
        echo "REFUSING: preserve incomplete/changed release for reviewed recovery: $RELEASE_DIR"; exit 1;
    }
else
    mkdir -p "$RELEASE_DIR/source" "$RELEASE_DIR/connector" "$RELEASE_DIR/units" "$RELEASE_DIR/dist"
    # Build from Git objects, never from mutable working-tree files.
    git archive --format=tar "$RELEASE_SHA" --output "$RELEASE_DIR/SOURCE.tar"
    tar -xf "$RELEASE_DIR/SOURCE.tar" -C "$RELEASE_DIR/source"
    git -C "$CONNECTOR_SRC" archive "$EXPECTED_CONNECTOR_SHA" | tar -xf - -C "$RELEASE_DIR/connector"
    printf '%s\n' "$RELEASE_SHA" > "$RELEASE_DIR/COMMIT"
    git rev-parse "$RELEASE_SHA^{tree}" > "$RELEASE_DIR/TREE"
    sha256sum "$RELEASE_DIR/SOURCE.tar" > "$RELEASE_DIR/SOURCE.sha256"
    cp "$RELEASE_DIR/source/src/prop_production/compatibility.json" "$RELEASE_DIR/compatibility.json"
    install -m 0644 "$RELEASE_DIR/source/deploy/"prop-production-*.service "$RELEASE_DIR/units/"
    "$UV" build --project "$RELEASE_DIR/source" --wheel --out-dir "$RELEASE_DIR/dist" --quiet
    UV_PROJECT_ENVIRONMENT="$RELEASE_DIR/venv" "$UV" sync --project "$RELEASE_DIR/source" \
        --python "$ROOT_PYTHON" --frozen --no-dev --no-editable --extra connector
    "$RELEASE_DIR/venv/bin/python" -c 'import ccxt; from prop_production.compatibility import BYBIT_CCXT; assert ccxt.__version__ == BYBIT_CCXT'
    chown -R root:root "$RELEASE_DIR"
    chmod -R go-w "$RELEASE_DIR"
    PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.deployment seal "$RELEASE_DIR"
    release_complete "$RELEASE_DIR"
fi
OPT_VENV="$RELEASE_DIR/venv"

# Service identities preserve the existing IPC uid contract.
getent group prop >/dev/null || groupadd --system prop
for role in runtime planner observer watchdog bybit; do
    id "prop-$role" >/dev/null 2>&1 || useradd --system --no-create-home \
        --shell /usr/sbin/nologin --gid prop "prop-$role"
done

# 4. Fixture runtime initialization: requires a non-existent state root.
if [ ! -e "$STATE" ]; then
    "$OPT_VENV/bin/python" -c "
from pathlib import Path
from prop_production.worker import initialize_fixture
print(initialize_fixture(Path('$STATE')))"
fi

# 5. Root-protected state tree; per-role ownership. Role dirs stay 0700:
# the worker Landlock sandbox rejects writable paths with group/other bits.
# Heartbeats live in role-owned health/ subdirs (0750 dir, 0640 file): only the
# owner role writes its own evidence; the watchdog and root read via group.
[ -d "$STATE/health/guardian" ] || mkdir -p "$STATE/health/guardian" "$STATE/health/observer" "$STATE/health/watchdog"
mkdir -p "$STATE/health/bybit" "$STATE/bybit"
chown root:prop "$STATE/health"; chmod 0755 "$STATE/health"
chown prop-runtime:prop "$STATE/health/guardian"; chmod 0750 "$STATE/health/guardian"
chown prop-observer:prop "$STATE/health/observer"; chmod 0750 "$STATE/health/observer"
chown prop-watchdog:prop "$STATE/health/watchdog"; chmod 0750 "$STATE/health/watchdog"
chown prop-bybit:prop "$STATE/health/bybit"; chmod 0750 "$STATE/health/bybit"
chown prop-bybit:prop "$STATE/bybit"; chmod 0700 "$STATE/bybit"
chown -R prop-runtime:prop "$STATE/broker" "$STATE/guardian" "$STATE/judge"
for role in broker guardian judge; do chmod 0700 "$STATE/$role"; done
chown -R prop-planner:prop "$STATE/planner"; chmod 0700 "$STATE/planner"
[ -d "$STATE/observer" ] || mkdir -p "$STATE/observer"
chown -R prop-observer:prop "$STATE/observer"; chmod 0750 "$STATE/observer"
[ -d "$STATE/watchdog" ] || mkdir -p "$STATE/watchdog"
chown -R prop-watchdog:prop "$STATE/watchdog"; chmod 0750 "$STATE/watchdog"


# Preserve a consistent online trust backup. Neither upgrade nor rollback restores
# an older trust floor or rewinds a financial journal.
if [ -f "$STATE/trust/floor.sqlite" ]; then
    BACKUP="$STATE/trust/floor.sqlite.pre-upgrade.$(date +%s%N)"
    PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.deployment backup \
        "$STATE/trust/floor.sqlite" --target "$BACKUP"
fi
chown root:prop "$STATE/trust"; chmod 0750 "$STATE/trust"
chown root:prop "$STATE/trust/settings.json" "$STATE/trust/floor.sqlite"
chmod 0640 "$STATE/trust/settings.json"
chmod 0440 "$STATE/trust/floor.sqlite"

# 6. Observer credentials: root-only file, value flows file-to-file only.
[ -f /etc/prop-production/observer.env ] || {
    mkdir -p /etc/prop-production
    grep '^PROPR_API_KEY=' /home/quant/dev/trading-connector/.env > /etc/prop-production/observer.env
    chown root:root /etc/prop-production/observer.env; chmod 0600 /etc/prop-production/observer.env
}

# Bybit requires an independently authenticated dedicated Demo key/UID.
# Never copy BYBIT_PROP_* or generic mainnet keys; never fall back to Testnet.
if [ ! -f /etc/prop-production/bybit.env ]; then
    install -m 0600 -o root -g root \
        /home/quant/.config/trading-connector/bybit-demo.env \
        /etc/prop-production/bybit.env
fi
chown root:root /etc/prop-production/bybit.env
chmod 0600 /etc/prop-production/bybit.env
grep -qx 'BYBIT_ENVIRONMENT=demo' /etc/prop-production/bybit.env || {
    echo "REFUSING: Bybit credentials are not for Demo"; exit 1;
}
grep -qx 'BYBIT_MODE=read_only' /etc/prop-production/bybit.env || {
    echo "REFUSING: Bybit observer credential mode not read_only"; exit 1;
}
for name in BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_DEMO_EXPECTED_UID; do
    grep -q "^$name=." /etc/prop-production/bybit.env || {
        echo "REFUSING: Bybit Demo credential field missing"; exit 1;
    }
done


# Activation snapshots old units and the old pointer, stops exactly these six
# services, switches one immutable code set, and verifies fresh role heartbeats.
# Failure restores the previous code+units. Interrupted activation has a durable
# recovery record; no financial database is ever restored automatically.
release_complete "$RELEASE_DIR"
PYTHONPATH="$REPO/src" "$ROOT_PYTHON" -m prop_production.deployment activate "$RELEASE_DIR" --lock-fd 9
systemctl enable prop-production-broker.service prop-production-guardian.service \
    prop-production-planner.service prop-production-observer.service prop-production-watchdog.service \
    prop-production-bybit-observer.service
printf 'DEPLOYED_SHADOW commit=%s\n' "$RELEASE_SHA"
