#!/bin/bash
# =================================================================
# EV Guardian — QRB2210 Standalone Launcher (No Laptop Required)
# =================================================================
# This script starts all backend services, registers the Arduino Uno Q,
# and displays the HTML5 3D Telemetry Dashboard in fullscreen Kiosk mode 
# directly on the connected 7-inch HDMI touchscreen TFT display.
# =================================================================

# 1. Ensure Mosquitto MQTT broker is running
echo "[SYSTEM] Starting MQTT Broker..."
sudo systemctl start mosquitto || sudo service mosquitto start

# Add local path to environment
cd "$(dirname "$0")"
export PYTHONPATH=$PYTHONPATH:.

# 2. Start the Gateway Daemon in HARDWARE mode to read from STM32 IPC
echo "[SYSTEM] Launching QRB2210 Gateway Daemon (HW IPC bridge)..."
python3 gateway/gateway_daemon.py --mode hw &
GATEWAY_PID=$!

# 3. Start the Core AI Engine / ONNX Inference Backend
echo "[SYSTEM] Launching ONNX Inference & Diagnostics Backend..."
python3 backend.py &
BACKEND_PID=$!

# 4. Start the Web Server to host the 3D Dashboard
echo "[SYSTEM] Launching Web Dashboard Server on port 8081..."
python3 serve_dashboard.py &
WEBSERVER_PID=$!

# 5. Wait a few seconds for services to fully initialize
echo "[SYSTEM] Warming up services..."
sleep 3

# 6. Launch Chromium in Fullscreen Kiosk Mode on the 7-inch TFT Display
# (Targeting the local physical Display :0 on Debian Linux)
echo "[DISPLAY] Starting fullscreen Kiosk dashboard on HDMI display..."
export DISPLAY=:0
chromium-browser --no-sandbox --kiosk --app=http://localhost:8081/index.html &
CHROMIUM_PID=$!

echo "================================================================="
echo "  EV Guardian Standalone Suite is running!"
echo "  Monitor active telemetry on the 7-inch TFT screen."
echo "  Press Ctrl+C to terminate all background daemons."
echo "================================================================="

# Wait and manage shutdown gracefully
trap "echo [STOP] Terminating all services...; kill $GATEWAY_PID $BACKEND_PID $WEBSERVER_PID $CHROMIUM_PID; exit" INT TERM
wait
