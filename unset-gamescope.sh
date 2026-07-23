#!/bin/bash

# The Gamescope config file for screen resolutions
CFG_FILE="/home/deck/.config/gamescope/modes.cfg"

# Ensure the file exists before trying to edit it
if [ ! -f "$CFG_FILE" ]; then
    echo "Error: $CFG_FILE does not exist."
    exit 1
fi

# Exit if there are no #autoset entries
if ! grep -q "#autoset" "$CFG_FILE"; then
    echo "No #autoset entries found in $CFG_FILE. Nothing to revert."
    exit 0
fi

# Find all unique display names with #autoset, and process them one by one
# awk extracts the name, sort -u ensures we only process each entry once
grep "#autoset" "$CFG_FILE" | awk -F':' '{print $1}' | sort -u | while read -r MONITOR_NAME; do
    echo "Processing restores for: $MONITOR_NAME"
    
    # Escape any periods in the monitor name so sed treats them literally
    ESCAPED_NAME=$(echo "$MONITOR_NAME" | sed 's/\./\\./g')

    # Look for lines starting with a hash followed by the exact display name,
    # and replace it with just the monitor name (removing the hash).
    sed -i "s/^#\(${ESCAPED_NAME}\)/\1/" "$CFG_FILE"
done

# Delete all lines containing #autoset
# The /d command in sed deletes any line matching the pattern
sed -i '/#autoset/d' "$CFG_FILE"
echo "Removed all #autoset lines from $CFG_FILE."

# Trigger gamescope to read the file and revert the resolution
gamescopectl backend_set_dirty
echo "Backend dirty flag set successfully."
