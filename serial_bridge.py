"""
EV Guardian — Serial to MQTT Bridge
====================================
This script reads the real telemetry output from the Arduino Uno Q via USB serial connection,
parses the values, and publishes them to the local MQTT broker at 10Hz.
This allows your real Arduino RTOS data to display live on the HTML/JS dashboard!
"""

import time
import json
import re
import sys
import serial
import serial.tools.list_ports
import paho.mqtt.client as mqtt

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "ev/sensor/telemetry"
MQTT_PRED_TOPIC = "ev/diagnostics/prediction"

# Global state for latest ML/AI diagnostics
latest_ml_pred = "NORMAL"
latest_ml_trust = 100.0
latest_ml_soh = 100.0
latest_ml_reason = ""

def find_arduino_port():
    """Scan all active COM ports for Arduino Uno Q or serial activity."""
    ports = list(serial.tools.list_ports.comports())
    print("\n[SERIAL] Scanning available ports:")
    arduino_ports = []
    for p in ports:
        print(f" - {p.device}: {p.description}")
        if "arduino" in p.description.lower() or "usb serial" in p.description.lower() or "ch340" in p.description.lower():
            arduino_ports.append(p.device)
    
    if arduino_ports:
        return arduino_ports[0]
    if ports:
        # Fallback to the first available COM port
        return ports[0].device
    return None

def parse_line(line):
    """
    Parses a serial line using regular expressions.
    Example line:
      C1: 4.162V | C2: 4.151V | C3: 4.200V | C4: 4.168V || CurrRaw: 5493 | CurrPin: 2.213V | Amps: 0.000A || T1: 26.5C | T2: 27.0C || CO: 45.2 ppm || Ax:0.012g Ay:-0.008g Az:0.998g | Gx:0.2d/s Gy:-0.1d/s Gz:0.0d/s | Vib:0.002g (0.020m/s2)
    """
    try:
        # Check if line contains telemetry markers
        if "C1:" not in line:
            return None

        # Helper regex puller
        def get_float(pattern, text):
            match = re.search(pattern, text)
            return float(match.group(1)) if match else 0.0

        c1 = get_float(r"C1:\s*([\d\.]+)", line)
        c2 = get_float(r"C2:\s*([\d\.]+)", line)
        c3 = get_float(r"C3:\s*([\d\.]+)", line)
        c4 = get_float(r"C4:\s*([\d\.]+)", line)

        # Get temperatures (T1, T2)
        # Note: can handle "T1: ERR" -> turns to 0.0
        t1 = get_float(r"T1:\s*([\d\.\-]+)", line) if "T1: ERR" not in line else 0.0
        t2 = get_float(r"T2:\s*([\d\.\-]+)", line) if "T2: ERR" not in line else 0.0

        current = get_float(r"Amps:\s*([\d\.\-]+)", line)
        
        # Vibration/Gas
        gas = get_float(r"CO:\s*([\d\.]+)", line)
        vib = get_float(r"Vib:\s*([\d\.]+)", line)

        # Build payload matching dummy_publisher structure
        payload = {
            "timestamp": int(time.time() * 1000),
            "device_id": "ev-uno-q-01",
            "cells": {
                "voltage_v": [c1, c2, c3, c4],
                "temp_c": [t1, t2, 0.0, 0.0]  # First two cell temp monitors
            },
            "pack": {
                "current_a": current,
                "vibration_g": vib,
                "gas_ppm": gas
            },
            "metadata": {
                "node_status": "OK"
            }
        }
        return payload
    except Exception as e:
        print(f"[PARSER ERR] Could not parse line: {e}")
        return None

def on_prediction_message(client, userdata, message):
    global latest_ml_pred, latest_ml_trust, latest_ml_soh, latest_ml_reason
    try:
        data = json.loads(message.payload.decode())
        latest_ml_pred = data.get("prediction", "NORMAL")
        latest_ml_trust = data.get("overall_trust", 100.0)
        latest_ml_soh = data.get("soh_pct", 100.0)
        latest_ml_reason = data.get("complexity_reason", "")
    except Exception:
        pass

def main():
    print("=" * 65)
    print("  EV Guardian — USB Serial to MQTT Bridge")
    print("=" * 65)

    # 1. Connect to MQTT
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "serial_bridge")
    mqtt_client.on_message = on_prediction_message
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.subscribe(MQTT_PRED_TOPIC)
        mqtt_client.loop_start()
        print(f"[MQTT] Connected successfully to {MQTT_BROKER}:{MQTT_PORT} and subscribed to '{MQTT_PRED_TOPIC}'")
    except Exception as e:
        print(f"[MQTT ERR] Connection failed: {e}")
        print("Please check that your Mosquitto MQTT Broker is running.")
        sys.exit(1)

    # 2. Get COM Port
    com_port = None
    if len(sys.argv) > 1:
        com_port = sys.argv[1]
    else:
        com_port = find_arduino_port()

    if not com_port:
        print("[SERIAL ERR] No COM Port found! Plug in your Arduino and try again.")
        sys.exit(1)

    print(f"[SERIAL] Attempting connection on port {com_port} @ 115200 baud...")
    
    # 3. Read from serial
    try:
        ser = serial.Serial(com_port, 115200, timeout=1.0)
        # Flush buffers
        ser.reset_input_buffer()
        print("[SERIAL] Port open! Streaming telemetry in real-time...")
    except Exception as e:
        print(f"[SERIAL ERR] Failed to open port {com_port}: {e}")
        print("Make sure no other serial monitor (like Arduino IDE) is using the port.")
        sys.exit(1)

    try:
        while True:
            if ser.in_waiting > 0:
                try:
                    raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not raw_line:
                        continue
                    
                    payload = parse_line(raw_line)
                    if payload:
                        # Publish telemetry
                        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
                        
                        # Format the ML telemetry print suffix if not already output by the device local firmware
                        if " || [ML/AI]" not in raw_line:
                            ml_msg = f" || [ML/AI] Prediction: {latest_ml_pred} | Overall Trust: {latest_ml_trust:.1f}% | SOH: {latest_ml_soh:.1f}%"
                            if latest_ml_reason:
                                ml_msg += f" (Alert: {latest_ml_reason})"
                            print(raw_line + ml_msg)
                        else:
                            print(raw_line)
                        
                        # Optionally write predictions back to Arduino for external hardware consumption
                        try:
                            ser.write(f"ML:{latest_ml_pred},{int(latest_ml_trust)},{int(latest_ml_soh)}\n".encode('utf-8'))
                        except Exception:
                            pass
                    else:
                        # Print generic boot messages, timing, or system notes
                        print(raw_line)
                except Exception as read_err:
                    print(f"\n[READ ERR] {read_err}")
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down Serial Bridge...")
    finally:
        ser.close()
        print("[STOP] Closed Serial Port.")

if __name__ == "__main__":
    main()
