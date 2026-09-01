# Install the pki-executor agent from the firstboot payload (Linux templates).
#
# A direct translation of 40-install-executor.ps1, which stays the authority on
# ordering and rationale: the v2 firstboot runner stages ISO payload `files` to
# a transient directory exported as $FIRSTBOOT_FILES_DIR and DELETES it
# afterwards, so this step copies the binary + config to persistent locations
# and registers the service. It does NOT start the service or reboot: the runner
# owns the single reboot, which brings the enabled unit up.
#
# Copy + install only — no role/product logic. Provisioning (the CertSecure
# install and everything around it) is dispatched by the backend after the agent
# phones home.

set -euo pipefail

if [ -z "${FIRSTBOOT_FILES_DIR:-}" ]; then
    # A pre-v2 runner never sets this (it ignores the manifest's `files`), so the
    # payload was never staged. Fail loudly rather than with a confusing path error.
    echo 'FIRSTBOOT_FILES_DIR is not set — this base image predates the v2 firstboot runner; rebuild the golden image before enabling executor bundling.' >&2
    exit 1
fi

# The mode is ours to set: the v2 Linux runner copies payload `files` with no
# execute bit (only *scripts* get one), so a plain copy here yields a binary the
# systemd unit cannot exec — which presents as an agent that never phones home
# rather than as anything about permissions.
install -D -m 0755 "$FIRSTBOOT_FILES_DIR/pki-executor" /usr/local/bin/pki-executor

# The config carries the agent's bearer token. This is the icacls equivalent:
# root-only, because every reader of this file can impersonate the agent.
install -D -o root -g root -m 0600 \
    "$FIRSTBOOT_FILES_DIR/executor.toml" /etc/pki-executor/config.toml

# The path below must match config.rs's `default_path()` for the non-Windows
# arm; it is passed explicitly anyway so the unit does not depend on that.
cat > /etc/systemd/system/pki-executor.service <<'UNIT'
[Unit]
Description=PKI executor agent
# The agent's first action is a phone-home WebSocket, so a start before the
# static address from 20-network.sh is up would burn its initial connect
# attempts on a box with no route.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/pki-executor connect --config /etc/pki-executor/config.toml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Enable, never start: the runner owns the single reboot, which is what brings
# this up — the same AutoStart contract the Windows service installer follows.
systemctl daemon-reload
systemctl enable pki-executor.service

echo 'pki-executor installed'
