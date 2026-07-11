import time
import json
import random
import paho.mqtt.client as mqtt

# MQTT Setup
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "ev/sensor/telemetry"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "dummy_publisher")

try:
    print(f"Connecting to MQTT Broker at {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    print("Successfully connected!")
except Exception as e:
    print(f"Connection failed: {e}")
    print("Please make sure Mosquitto MQTT Broker is installed and running on port 1883.")
    print("Continuing execution (will attempt to reconnect)...")

print(f"Simulating EV Telemetry at 10Hz. Publishing to topic: '{MQTT_TOPIC}'")
print("States cycle every 10 seconds: NORMAL -> FAULT (Cell 3 drop & high temp) -> NORMAL...")

start_time = time.time()

while True:
    elapsed = time.time() - start_time
    # Alternate state every 10 seconds
    is_fault_state = (int(elapsed // 10) % 2) == 1

    timestamp = int(time.time() * 1000)
    
    if is_fault_state:
        # Simulate Cell 3 wire disconnect / thermal reporting fault
        voltages = [
            round(random.uniform(3.80, 3.84), 2),
            round(random.uniform(3.80, 3.84), 2),
            1.25, # Fault voltage
            round(random.uniform(3.80, 3.84), 2)
        ]
        temperatures = [
            round(random.uniform(34.0, 34.5), 1),
            round(random.uniform(34.0, 34.5), 1),
            115.2, # Fault temperature
            round(random.uniform(34.0, 34.5), 1)
        ]
        current = round(random.uniform(-13.0, -12.0), 1)
        vibration = round(random.uniform(0.06, 0.10), 2)
        gas = random.randint(10, 15)
        state_label = "FAULT STATE (Cell 3 Wire Disconnect)"
    else:
        # Simulate healthy operations
        voltages = [
            round(random.uniform(3.80, 3.84), 2),
            round(random.uniform(3.80, 3.84), 2),
            round(random.uniform(3.78, 3.82), 2),
            round(random.uniform(3.80, 3.84), 2)
        ]
        temperatures = [
            round(random.uniform(34.0, 34.5), 1),
            round(random.uniform(34.0, 34.5), 1),
            round(random.uniform(34.0, 34.5), 1),
            round(random.uniform(34.0, 34.5), 1)
        ]
        current = round(random.uniform(-2.5, -1.0), 1)
        vibration = round(random.uniform(0.02, 0.05), 2)
        gas = random.randint(3, 8)
        state_label = "HEALTHY STATE"

    payload = {
        "timestamp": timestamp,
        "device_id": "ev-uno-q-01",
        "cells": {
            "voltage_v": voltages,
            "temp_c": temperatures
        },
        "pack": {
            "current_a": current,
            "vibration_g": vibration,
            "gas_ppm": gas
        },
        "metadata": {
            "node_status": "OK" if not is_fault_state else "SENSOR_DISCONNECT"
        }
    }

    try:
        client.publish(MQTT_TOPIC, json.dumps(payload))
        print(f"[{state_label}] Published: Volts={voltages}, Temp={temperatures}, Current={current}A, Vib={vibration}G, Gas={gas}ppm", end="\r")
    except Exception as e:
        print(f"\nPublish failed: {e}")

    time.sleep(0.1) # 10Hz transmission rate
