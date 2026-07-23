#!/bin/bash

# 1. Determine which port to use (HDMI-A-1 first, DP-1 fallback)
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

# Exit if neither port is connected
if [ -z "$PORT_PATH" ]; then
    echo "Error: Neither HDMI-A-1 nor DP-1 are connected."
    exit 1
fi

EDID_FILE="$PORT_PATH/edid"

# 2. Extract the Manufacturer Code and Display Product Name from the EDID
# $2 gets the second word (the 3-letter code). tr removes any single quotes around the model name.
MFG_CODE=$(edid-decode "$EDID_FILE" 2>/dev/null | grep -m1 "Manufacturer:" | awk '{print $2}')
MODEL_NAME=$(edid-decode "$EDID_FILE" 2>/dev/null | grep -m1 "Display Product Name:" | awk -F': ' '{print $2}' | tr -d "'")

if [ -z "$MFG_CODE" ] || [ -z "$MODEL_NAME" ]; then
    echo "Error: Could not extract Manufacturer or Model from EDID."
    exit 1
fi

# 3. Look up the full manufacturer name in the pnp.ids database
PNP_FILE="/usr/share/hwdata/pnp.ids"
FULL_MFG="$MFG_CODE" # Default to the 3-letter code if lookup fails

if [ -f "$PNP_FILE" ]; then
    # Search for the line starting with the code followed by whitespace, then strip the code and whitespace
    LOOKUP=$(grep -m1 -E "^${MFG_CODE}[[:space:]]+" "$PNP_FILE" | sed -E "s/^${MFG_CODE}[[:space:]]+//")
    if [ -n "$LOOKUP" ]; then
        FULL_MFG="$LOOKUP"
    fi
fi

# Construct the final monitor string (e.g., "Dell Inc. DELL S3423DWC")
MONITOR_NAME="$FULL_MFG $MODEL_NAME"
echo "Detected Monitor: $MONITOR_NAME"

# 4. Edit the Gamescope config file
CFG_FILE="/home/deck/.config/gamescope/modes.cfg"

# Ensure the directory and file exist
mkdir -p "$(dirname "$CFG_FILE")"
touch "$CFG_FILE"

# Escape periods in the monitor name so sed treats them as literal characters, not regex wildcards
ESCAPED_NAME=$(echo "$MONITOR_NAME" | sed 's/\./\\./g')

# Look for any line starting with the exact monitor name and prepend a hash (#)
sed -i "s/^${ESCAPED_NAME}/#&/" "$CFG_FILE"

# Append the new autoset line
echo "${MONITOR_NAME}:1280x800@60 0 #autoset" >> "$CFG_FILE"
echo "Updated $CFG_FILE"

# 5. Run the final command
gamescopectl backend_set_dirty
echo "Backend dirty flag set successfully."
