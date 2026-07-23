#!/bin/bash

# The Gamescope config file for screen resolutions
CFG_FILE="/home/deck/.config/gamescope/modes.cfg"

# Systemd Headless Environment Injection
STEAM_PID=$(pgrep -u deck -x steam | head -n 1)
if [ -n "$STEAM_PID" ]; then
    export $(tr '\0' '\n' < /proc/$STEAM_PID/environ | grep -E '^(WAYLAND_DISPLAY|DISPLAY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)=')
fi

# Decide what action to take ( SET || UNSET )
if [ -f "/home/deck/.trigger-gamescope-set" ]; then
    ACTION="set"
    rm -f "/home/deck/.trigger-gamescope-set"
    rm -f "/home/deck/.trigger-gamescope-unset" # ensure sure this isn't present after SET
elif [ -f "/home/deck/.trigger-gamescope-unset" ]; then
    ACTION="unset"
    rm -f "/home/deck/.trigger-gamescope-unset"
else
    exit 0
fi

# --- Action: SET ---
if [ "$ACTION" == "set" ]; then
    PORT_PATH=""
    for p in /sys/class/drm/*-HDMI-A-1; do
        if [ -f "$p/status" ] && [ "$(cat "$p/status" 2>/dev/null)" = "connected" ]; then
            PORT_PATH="$p"
            break
        fi
    done
    if [ -z "$PORT_PATH" ]; then
        for p in /sys/class/drm/*-DP-1; do
            if [ -f "$p/status" ] && [ "$(cat "$p/status" 2>/dev/null)" = "connected" ]; then
                PORT_PATH="$p"
                break
            fi
        done
    fi
    if [ -n "$PORT_PATH" ]; then
        EDID_FILE="$PORT_PATH/edid"
        MFG_CODE=$(edid-decode "$EDID_FILE" 2>/dev/null | grep -m1 "Manufacturer:" | awk '{print $2}')
        MODEL_NAME=$(edid-decode "$EDID_FILE" 2>/dev/null | grep -m1 "Display Product Name:" | awk -F': ' '{print $2}' | tr -d "'")
        
        PNP_FILE="/usr/share/hwdata/pnp.ids"
        FULL_MFG="$MFG_CODE"
        if [ -f "$PNP_FILE" ]; then
            LOOKUP=$(grep -m1 -E "^${MFG_CODE}[[:space:]]+" "$PNP_FILE" | sed -E "s/^${MFG_CODE}[[:space:]]+//")
            if [ -n "$LOOKUP" ]; then
                FULL_MFG="$LOOKUP"
            fi
        fi

        DISPLAY_NAME="$FULL_MFG $MODEL_NAME"
        mkdir -p "$(dirname "$CFG_FILE")"
        touch "$CFG_FILE"
        ESCAPED_NAME=$(echo "$DISPLAY_NAME" | sed 's/\./\\./g')
        sed -i "s/^${ESCAPED_NAME}/#&/" "$CFG_FILE"
        echo "${DISPLAY_NAME}:1280x800@60 0 #autoset" >> "$CFG_FILE"
        gamescopectl backend_set_dirty
    fi

# --- Action: UNSET ---
elif [ "$ACTION" == "unset" ]; then
    if [ -f "$CFG_FILE" ] && grep -q "#autoset" "$CFG_FILE"; then
        grep "#autoset" "$CFG_FILE" | awk -F':' '{print $1}' | sort -u | while read -r DISPLAY_NAME; do
            ESCAPED_NAME=$(echo "$DISPLAY_NAME" | sed 's/\./\\./g')
            sed -i "s/^#\(${ESCAPED_NAME}\)/\1/" "$CFG_FILE"
        done
        sed -i '/#autoset/d' "$CFG_FILE"
        gamescopectl backend_set_dirty
    fi
fi

