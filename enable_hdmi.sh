#!/bin/bash
echo on | sudo tee /sys/kernel/debug/dri/0/HDMI-A-1/force && sudo udevadm trigger --subsystem-match=drm --action=change
