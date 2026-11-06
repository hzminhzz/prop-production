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

# 2. Application venv from the reviewed commit's checkout.
[ -d "$OPT" ] || mkdir -p "$OPT"
[ -d "$OPT/venv" ] || python3 -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install --quiet "$REPO" python-dotenv httpx

# 3. Pinned connector code for the observer (SHA recorded next to the copy).
mkdir -p "$OPT/connector/connectors"
cp -r "$CONNECTOR_SRC/connectors/propr" "$OPT/connector/connectors/propr"
sha256sum "$CONNECTOR_SRC/connectors/propr/"*.py > "$OPT/connector/PIN.sha256"

# 4. Fixture runtime initialization: requires a non-existent state root.
if [ ! -e "$STATE" ]; then
    "$OPT/venv/bin/python" -c "
from pathlib import Path
from prop_production.worker import initialize_fixture
print(initialize_fixture(Path('$STATE')))"
fi

# 5. Root-protected state tree; per-role ownership. Role dirs stay 0700:
# the worker Landlock sandbox rejects writable paths with group/other bits.
# Shared heartbeats live in health/ (0770 root:prop, files 0640).
[ -d "$STATE/health" ] || mkdir -p "$STATE/health"
chown root:prop "$STATE"; chmod 0750 "$STATE"
chown root:prop "$STATE/health"; chmod 0770 "$STATE/health"
chown -R prop-runtime:prop "$STATE/broker" "$STATE/guardian" "$STATE/judge"
for role in broker guardian judge; do chmod 0700 "$STATE/$role"; done
chown -R prop-planner:prop "$STATE/planner"; chmod 0700 "$STATE/planner"
[ -d "$STATE/observer" ] || mkdir -p "$STATE/observer"
chown -R prop-observer:prop "$STATE/observer"; chmod 0750 "$STATE/observer"
[ -d "$STATE/watchdog" ] || mkdir -p "$STATE/watchdog"
chown -R prop-watchdog:prop "$STATE/watchdog"; chmod 0750 "$STATE/watchdog"

# Trust anchor: group-readable; the SQLite WAL reader needs write on the
# -wal/-shm side files (dir stays 0750), never on settings or keys.
chown -R root:prop "$STATE/trust"; chmod 0750 "$STATE/trust"
chmod 0640 "$STATE/trust/settings.json" 2>/dev/null || true
for suffix in "" -wal -shm; do
    [ -f "$STATE/trust/floor.sqlite$suffix" ] && chmod 0660 "$STATE/trust/floor.sqlite$suffix"
done

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

echo "DEPLOYED: systemctl status prop-production-*"
