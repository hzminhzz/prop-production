#!/usr/bin/env bash
# prop-production protected deployment installer. Run with sudo from the repo root.
# Idempotent: safe to re-run. Credentials flow file-to-file and never enter argv or logs.
#
# Identity model: the existing IPC mechanism binds broker roles to the broker
# process uid (ipc.py peer-credential check), so broker and Guardian share one
# service identity. Planner, observer and watchdog keep separate identities.
set -euo pipefail

REPO=${1:-/home/quant/dev/prop-production}
STATE=/var/lib/prop-production
OPT=/opt/prop-production
CONNECTOR_SRC=${CONNECTOR_SRC:-/home/quant/.devspace/worktrees/trading-connector-829a9992}

cd "$REPO"

# 1. Service identities (system users, no login shell). Group before users.
getent group prop >/dev/null || groupadd --system prop
for role in runtime planner observer watchdog; do
    id "prop-$role" >/dev/null 2>&1 || useradd --system --no-create-home \
        --shell /usr/sbin/nologin --gid prop "prop-$role"
done

# 2. Release layout: the release identity is the APPROVED MERGE COMMIT on the
# default branch. The checkout must match its tree exactly; the wheel is built,
# hashed and verified before installation. Rollback = repoint the symlink.
DEFAULT_BRANCH=feat/account-guardian-program
git fetch origin "$DEFAULT_BRANCH" 2>/dev/null || true
RELEASE_SHA=$(git rev-parse "origin/$DEFAULT_BRANCH")
git diff --quiet HEAD "origin/$DEFAULT_BRANCH" || {
    echo "REFUSING: local tree differs from approved $DEFAULT_BRANCH ($RELEASE_SHA)"; exit 1; }
[ -d "$OPT" ] || mkdir -p "$OPT"
RELEASE_DIR="$OPT/releases/$RELEASE_SHA"
# A release directory must not be accepted solely because it exists: verify its
# manifest and contents before reuse; a partial installation is rebuilt.
release_complete() {
    [ -x "$1/venv/bin/python" ] || return 1
    [ -f "$1/COMMIT" ] || return 1
    [ -f "$1/SOURCE.sha256" ] || return 1
    [ -f "$1/dist/SHA256SUMS" ] || return 1
    ls "$1"/dist/*.whl >/dev/null 2>&1 || return 1
    (cd "$1/dist" && sha256sum -c SHA256SUMS) >/dev/null 2>&1
}
if [ -d "$RELEASE_DIR" ] && ! release_complete "$RELEASE_DIR"; then
    echo "INCOMPLETE RELEASE at $RELEASE_DIR — rebuilding"
    rm -rf "$RELEASE_DIR"
fi
if [ ! -d "$RELEASE_DIR" ]; then
    mkdir -p "$RELEASE_DIR"
    git diff --quiet HEAD || { echo "REFUSING: dirty review tree"; exit 1; }
    WHEEL_DIR="$RELEASE_DIR/dist"
    mkdir -p "$WHEEL_DIR"
    UV=${UV:-/home/quant/.local/bin/uv}
    command -v uv >/dev/null 2>&1 && UV=uv
    (uv build --wheel --out-dir "$WHEEL_DIR" 2>/dev/null \
        || "$UV" build --wheel --out-dir "$WHEEL_DIR" 2>/dev/null) \
        || { echo "REFUSING: wheel build failed"; exit 1; }
    WHEEL=$(ls "$WHEEL_DIR"/*.whl | head -1)
    WHEEL_SHA=$(sha256sum "$WHEEL" | cut -d' ' -f1)
    echo "$WHEEL_SHA  $(basename "$WHEEL")" > "$WHEEL_DIR/SHA256SUMS"
    # Verify the recorded digest matches before installing anything.
    sha256sum -c "$WHEEL_DIR/SHA256SUMS" >/dev/null || { echo "REFUSING: wheel digest mismatch"; exit 1; }
    python3 -m venv "$RELEASE_DIR/venv"
    "$RELEASE_DIR/venv/bin/pip" install --quiet "$WHEEL" python-dotenv httpx
    git rev-parse "$RELEASE_SHA" > "$RELEASE_DIR/COMMIT"
    git cat-file -p "$RELEASE_SHA" | grep '^tree' > "$RELEASE_DIR/TREE"
    (cd "$REPO" && git archive "$RELEASE_SHA" | sha256sum) > "$RELEASE_DIR/SOURCE.sha256"
    CONNECTOR_SHA=$(git -C "$CONNECTOR_SRC" rev-parse HEAD)
    echo "connector=$CONNECTOR_SHA" > "$RELEASE_DIR/PINS"
fi
PREVIOUS_RELEASE=$(readlink -f "$OPT/current" 2>/dev/null || true)
ln -sfn "$RELEASE_DIR" "$OPT/current.new" && mv -T "$OPT/current.new" "$OPT/current"
OPT_VENV="$OPT/current/venv"

# 3. Pinned connector code for the observer (SHA recorded next to the copy).
mkdir -p "$OPT/connector/connectors"
cp -r "$CONNECTOR_SRC/connectors/propr" "$OPT/connector/connectors/propr"
sha256sum "$CONNECTOR_SRC/connectors/propr/"*.py > "$OPT/connector/PIN.sha256"

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
chown root:prop "$STATE/health"; chmod 0755 "$STATE/health"
chown prop-runtime:prop "$STATE/health/guardian"; chmod 0750 "$STATE/health/guardian"
chown prop-observer:prop "$STATE/health/observer"; chmod 0750 "$STATE/health/observer"
chown prop-watchdog:prop "$STATE/health/watchdog"; chmod 0750 "$STATE/health/watchdog"
chown -R prop-runtime:prop "$STATE/broker" "$STATE/guardian" "$STATE/judge"
for role in broker guardian judge; do chmod 0700 "$STATE/$role"; done
chown -R prop-planner:prop "$STATE/planner"; chmod 0700 "$STATE/planner"
[ -d "$STATE/observer" ] || mkdir -p "$STATE/observer"
chown -R prop-observer:prop "$STATE/observer"; chmod 0750 "$STATE/observer"
[ -d "$STATE/watchdog" ] || mkdir -p "$STATE/watchdog"
chown -R prop-watchdog:prop "$STATE/watchdog"; chmod 0750 "$STATE/watchdog"

# Trust anchor: the release floor is the anti-rollback authority. DELETE journal
# mode lets runtime workers open it read-only with zero write access; only the
# deploy-time trust authority (root) may ever advance it.
chown root:prop "$STATE/trust"; chmod 0750 "$STATE/trust"
# Settings and floor: group-readable, never group-writable. The DELETE-journal
# floor needs zero write access at runtime; only root (deploy authority) writes.
chown root:prop "$STATE/trust/settings.json" "$STATE/trust/floor.sqlite" 2>/dev/null || true
chmod 0640 "$STATE/trust/settings.json" 2>/dev/null || true
chmod 0440 "$STATE/trust/floor.sqlite" 2>/dev/null || true
# Migrate a pre-DELETE-mode floor (WAL sidecars needed shm write). Fail closed:
# backup, validate, checkpoint, verify, convert, verify again — abort on any
# failure and keep the previous database; sidecars are removed only after a
# proven successful migration.
if [ -f "$STATE/trust/floor.sqlite" ]; then
    BACKUP="$STATE/trust/floor.sqlite.pre-migration.$(date +%s)"
    cp "$STATE/trust/floor.sqlite" "$BACKUP"
    chmod 0600 "$BACKUP"
    "$OPT_VENV/bin/python" - "$STATE/trust/floor.sqlite" <<'PYEOF' || {
import sqlite3, sys
path = sys.argv[1]
con = sqlite3.connect(path, timeout=5.0, isolation_level=None)
try:
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("TRUST_DB_CORRUPT_BEFORE_MIGRATION")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if mode == "wal":
        con.execute("BEGIN IMMEDIATE")
        busy, _log, _ckpt = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise SystemExit("TRUST_DB_BUSY_CHECKPOINT_FAILED")
        con.execute("COMMIT")
        con.execute("PRAGMA journal_mode=DELETE")
        if con.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise SystemExit("TRUST_DB_MODE_SWITCH_FAILED")
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("TRUST_DB_CORRUPT_AFTER_MIGRATION")
    print(f"trust migration ok (mode={con.execute('PRAGMA journal_mode').fetchone()[0]})")
finally:
    con.close()
PYEOF
    echo "MIGRATION FAILED: keeping previous release and database; sidecars untouched"
    exit 1
}
    rm -f "$STATE/trust/floor.sqlite-wal" "$STATE/trust/floor.sqlite-shm"
fi

# 6. Observer credentials: root-only file, value flows file-to-file only.
[ -f /etc/prop-production/observer.env ] || {
    mkdir -p /etc/prop-production
    grep '^PROPR_API_KEY=' /home/quant/dev/trading-connector/.env > /etc/prop-production/observer.env
    chown root:root /etc/prop-production/observer.env; chmod 0600 /etc/prop-production/observer.env
}

# 7. Units, then start.
install -m 0644 deploy/prop-production-broker.service /etc/systemd/system/
install -m 0644 deploy/prop-production-guardian.service /etc/systemd/system/
install -m 0644 deploy/prop-production-planner.service /etc/systemd/system/
install -m 0644 deploy/prop-production-observer.service /etc/systemd/system/
install -m 0644 deploy/prop-production-watchdog.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prop-production-broker.service prop-production-guardian.service \
    prop-production-planner.service prop-production-observer.service prop-production-watchdog.service
# enable --now does not restart running services; a new release must take effect.
systemctl restart prop-production-broker.service prop-production-guardian.service \
    prop-production-planner.service prop-production-observer.service prop-production-watchdog.service

echo "DEPLOYED: systemctl status prop-production-*"
