#!/bin/sh
# Sync the plugin tree to the Dev OPNsense and reload the configd /
# webgui so the changes take effect, without a full pkg rebuild.
set -eu

DEV_HOST="${DEV_HOST:-opnsense-dev}"
DEV_PASS="${DEV_PASS:-JKshPJ4t}"
SRC="$(dirname "$0")/src/opnsense"
REMOTE="/usr/local/opnsense"

if ! command -v sshpass >/dev/null 2>&1; then
    echo "sshpass missing — install on this host first." >&2
    exit 1
fi

# IMPORTANT: rsync without --delete to avoid wiping out the live OPNsense
# tree under /usr/local/opnsense/ when our plugin only ships a subset.
# We use a per-file checksum compare instead, which is fine for the
# handful of small files this plugin contributes.
echo "==> Syncing $SRC → $DEV_HOST:$REMOTE  (additive, no --delete)"
SSHPASS="$DEV_PASS" sshpass -e rsync -avc \
    --rsh='ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no' \
    "$SRC/" root@"$DEV_HOST":"$REMOTE/"

# Cleanup of obsolete plugin files between deploys is the developer's
# responsibility — explicit rm via SSH if you rename a controller, etc.

echo "==> Restart configd + webgui"
SSHPASS="$DEV_PASS" sshpass -e ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
    root@"$DEV_HOST" '
        chmod +x /usr/local/opnsense/scripts/OPNsense/Fleet/agent.py
        /usr/local/etc/rc.d/configd restart
        sleep 1
        configctl webgui restart
    '
echo "==> Done. UI should be at https://$DEV_HOST/ui/fleet/index"
