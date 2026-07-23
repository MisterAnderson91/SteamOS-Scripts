#!/bin/bash

# Define the config file path
CONFIG_FILE="$HOME/.config/kwinoutputconfig.json"

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "Error: 'jq' is not installed. Please install it first (e.g., sudo apt install jq or sudo dnf install jq)."
    exit 1
fi

# Check if the config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Configuration file not found at $CONFIG_FILE"
    exit 1
fi

# Use jq to find the display with priority 0 (the primary display)
# 1. Iterate through the setups (.[] | . as $config)
# 2. Look at the outputs array (.outputs[]?)
# 3. Filter for priority 0 (select(.priority == 0))
# 4. Map the outputIndex back to the data array to get the connectorName
PRIMARY_DISPLAY=$(jq -r '
  .[] | 
  . as $config | 
  .outputs[]? | 
  select(.priority == 0) | 
  $config.data[.outputIndex].connectorName
' "$CONFIG_FILE" | head -n 1)

# Handle cases where parsing fails or returns null
if [ -z "$PRIMARY_DISPLAY" ] || [ "$PRIMARY_DISPLAY" == "null" ]; then
    echo "Could not determine the primary display. Is the configuration file properly populated?"
    exit 1
fi

# Output the results
echo "Primary display detected: $PRIMARY_DISPLAY"

if [[ "$PRIMARY_DISPLAY" == *"HDMI-A-1"* ]]; then
    echo "Result: HDMI-A-1 is your primary display."
elif [[ "$PRIMARY_DISPLAY" == *"DP-1"* ]]; then
    echo "Result: DP-1 is your primary display."
else
    echo "Result: Neither HDMI-A-1 nor DP-1 is primary (Found: $PRIMARY_DISPLAY)."
fi
