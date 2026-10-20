#!/usr/bin/env bash
# Unbind apple-mfi-fastcharge kernel driver from any connected Apple devices.
# The driver locks interface 0 on 05ac devices, blocking libusb from claiming DFU/Pongo endpoints.
set -euo pipefail

UNBOUND=0
for dev in /sys/bus/usb/devices/*; do
    [ -f "$dev/idVendor" ] || continue
    vid=$(cat "$dev/idVendor")
    [ "$vid" = "05ac" ] || continue
    name=$(basename "$dev")
    if [ -d "/sys/bus/usb/drivers/apple-mfi-fastcharge" ]; then
        if [ -e "/sys/bus/usb/drivers/apple-mfi-fastcharge/$name" ]; then
            echo "$name" > /sys/bus/usb/drivers/apple-mfi-fastcharge/unbind 2>/dev/null || true
            echo "[*] Unbound apple-mfi-fastcharge from $name"
            UNBOUND=$((UNBOUND + 1))
        fi
    fi
done

if [ "$UNBOUND" -eq 0 ]; then
    echo "[*] No active apple-mfi-fastcharge claims found."
fi
