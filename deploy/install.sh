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

# 2. Release layout: build from the REVIEWED COMMIT, hash the artifact, install
# to releases/<sha>/ and flip the current symlink atomically. Rollback = repoint.
REVIEWED_SHA=$(git rev-parse HEAD)
[ -d "$OPT" ] || mkdir -p "$OPT"
RELEASE_DIR="$OPT/releases/$REVIEWED_SHA"
if [ ! -d "$RELEASE_DIR" ]; then
    mkdir -p "$RELEASE_DIR"
    git diff --quiet HEAD || { echo "REFUSING: dirty review tree"; exit 1; }
    python3 -m venv "$RELEASE_DIR/venv"
    "$RELEASE_DIR/venv/bin/pip" install --quiet "$REPO" python-dotenv httpx
    git rev-parse HEAD > "$RELEASE_DIR/COMMIT"
    (cd "$REPO" && git archive HEAD | sha256sum) > "$RELEASE_DIR/SOURCE.sha256"
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
# Migrate a pre-DELETE-mode floor (WAL sidecars needed shm write): checkpoint,
# convert, then drop the sidecars so runtime read-only opens need zero writes.
"$OPT_VENV/bin/python" -c "
import sqlite3
con = sqlite3.connect('$STATE/trust/floor.sqlite', timeout=0.1, isolation_level=None)
mode = con.execute('PRAGMA journal_mode').fetchone()[0]
if mode == 'wal':
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    con.execute('PRAGMA journal_mode=DELETE')
con.close()
" || true
rm -f "$STATE/trust/floor.sqlite-wal" "$STATE/trust/floor.sqlite-shm"

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
