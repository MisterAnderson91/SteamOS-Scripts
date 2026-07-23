#!/bin/bash

CFG_FILE="/home/deck/.config/gamescope/modes.cfg"

# Ensure the file exists before trying to edit it
if [ ! -f "$CFG_FILE" ]; then
    echo "Error: $CFG_FILE does not exist."
    exit 1
fi

# Check if there are any #autoset entries at all
if ! grep -q "#autoset" "$CFG_FILE"; then
    echo "No #autoset entries found in $CFG_FILE. Nothing to revert."
    exit 0
fi

# 1. Find all unique monitor names with #autoset, and process them one by one
# awk extracts the name, sort -u ensures we only process each monitor once
grep "#autoset" "$CFG_FILE" | awk -F':' '{print $1}' | sort -u | while read -r MONITOR_NAME; do
    echo "Processing restores for: $MONITOR_NAME"
    
    # Escape any periods in the monitor name so sed treats them literally
    ESCAPED_NAME=$(echo "$MONITOR_NAME" | sed 's/\./\\./g')

    # Look for a line starting with a hash (^#) followed by the exact monitor name,
    # and replace it with just the monitor name (removing the hash).
    sed -i "s/^#\(${ESCAPED_NAME}\)/\1/" "$CFG_FILE"
done

# 2. Delete all lines containing #autoset globally
# The /d command in sed deletes any line matching the pattern
sed -i '/#autoset/d' "$CFG_FILE"
echo "Removed all #autoset lines from $CFG_FILE."

# 3. Run the final command
gamescopectl backend_set_dirty
echo "Backend dirty flag set successfully."
