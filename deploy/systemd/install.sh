#!/usr/bin/env bash
# Install the ApplyPilot user timers. Idempotent.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$UNIT_DIR"
for unit in jobpipe-prepare.service jobpipe-prepare.timer \
            jobpipe-otp.service jobpipe-otp.timer; do
    install -m 0644 "$SRC/$unit" "$UNIT_DIR/$unit"
    echo "installed $unit"
done

systemctl --user daemon-reload
systemctl --user enable --now jobpipe-prepare.timer
echo
echo "jobpipe-prepare.timer enabled."
echo "jobpipe-otp.timer NOT enabled — it fails until Gmail auth exists."
echo "Enable it with: systemctl --user enable --now jobpipe-otp.timer"
echo
systemctl --user list-timers 'jobpipe-*' --no-pager || true
