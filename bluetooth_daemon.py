"""
EV Guardian - Bluetooth Companion Sync Daemon (Snapdragon PC Edge)
===================================================================
Establishes a local Bluetooth RFCOMM server channel on the Snapdragon PC. 
Subscribes to the MQTT telemetry stream and relays structured JSON packets 
(and local LLM diagnostic alerts) to the connected Smartphone app.

Prerequisites:
  - Python 3.9+ (Windows supports native socket.AF_BLUETOOTH)
  - paho-mqtt
"""

import socket
import json
import time
import threading
import requests
import paho.mqtt.client as mqtt

# CONFIGURATION
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "ev/telemetry"
LLM_API_URL = "http://127.0.0.1:8766/diagnose"

# Global states
client_connected = False
client_socket = None
last_sent_time = 0
last_telemetry = None

def get_local_llm_diagnosis(reason):
    """Hits the local Snapdragon PC diagnostic LLM server endpoint"""
    try:
        print(f"[LLM] Prompting local LLM for failure event: {reason}")
        response = requests.get(f"{LLM_API_URL}?reason={requests.utils.quote(reason)}", timeout=15)
        if response.status_code == 200:
            return response.json().get("diagnosis", "Error compiling diagnosis.")
    except Exception as e:
        print(f"[LLM] Error reaching local LLM server: {e}")
    
    # Offline backup payload
    return (
        f"[LOCAL HARDWARE ENVELOPE REPORT]\n"
        f"Critical telemetry breach: {reason}\n"
        f"Action Recommended:\n"
        f"1. Cut power relay immediately.\n"
        f"2. Run localized cell temperature isolation protocols (STM32 side).\n"
        f"3. Run physical wire continuity checks on cell balance block."
    )

def on_mqtt_message(client, userdata, message):
    """Callback when a telemetry frame arrives on the MQTT local bus"""
    global client_socket, client_connected, last_sent_time, last_telemetry
    
    try:
        payload = json.loads(message.payload.decode("utf-8"))
        last_telemetry = payload
        
        # If a bluetooth client is connected, relay the packet (rate-limited to 1Hz to reduce BT bandwidth load)
        if client_connected and client_socket:
            current_time = time.time()
            
            # Check for anomalies immediately (override rate limit)
            is_anomaly = payload.get("is_anomaly", False)
            
            if is_anomaly or (current_time - last_sent_time >= 1.0):
                data_packet = {
                    "type": "telemetry",
                    "timestamp": payload.get("timestamp", int(time.time() * 1000)),
                    "voltages": payload.get("voltages", [0,0,0,0]),
                    "temperatures": payload.get("temperatures", [0,0,0,0]),
                    "current": payload.get("current_a", 0.0),
                    "soh_pct": payload.get("soh_pct", 100.0),
                    "is_anomaly": is_anomaly
                }
                
                # If an anomaly is detected, trigger the local LLM immediately and package it in the bluetooth envelope
                if is_anomaly:
                    reason = payload.get("alert_reason", "MODEL_ANOMALY_TRIGGER")
                    print(f"\n[ALERT] Active fault detected via NPU. Running LLM explanation...")
                    diagnosis = get_local_llm_diagnosis(reason)
                    
                    data_packet["type"] = "alert"
                    data_packet["reason"] = reason
                    data_packet["diagnosis"] = diagnosis
                
                # Send to smartphone client
                send_to_bluetooth(data_packet)
                last_sent_time = current_time
    except Exception as e:
         print(f"[MQTT] Packet parsing error: {e}")

def send_to_bluetooth(data):
    """Sends serialized JSON packet with terminating newline over RFCOMM"""
    global client_socket, client_connected
    try:
        payload_str = json.dumps(data) + "\n"
        client_socket.send(payload_str.encode("utf-8"))
        if data["type"] == "alert":
            print(f"[BLUETOOTH] Relayed active LLM Diagnosis to smartphone!")
        else:
            print(f"[BLUETOOTH] Relayed telemetry update: V={data['voltages']} SOH={data['soh_pct']:.1f}%")
    except Exception as e:
        print(f"[BLUETOOTH] Send failed, client disconnected: {e}")
        client_connected = False
        if client_socket:
            try:
                client_socket.close()
            except:
                pass
            client_socket = None

def run_mqtt():
    """Worker thread to run the local MQTT client loop"""
    print("[MQTT] Connecting to local Snapdragon MQTT broker...")
    mqtt_client = mqtt.Client()
    mqtt_client.on_message = on_mqtt_message
    
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.subscribe(MQTT_TOPIC)
        print(f"[MQTT] Successfully subscribed to topic '{MQTT_TOPIC}'")
        mqtt_client.loop_forever()
    except Exception as e:
        print(f"[MQTT] Connection loop crashed: {e}")

def run_bluetooth_server():
    """Main thread Bluetooth RFCOMM port listener"""
    global client_socket, client_connected
    
    while True:
        try:
            # Native RFCOMM setup
            # AF_BLUETOOTH is native in Windows/Linux Python
            server_sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            
            # MAC address "00:00:00:00:00:00" binds to local Bluetooth controller adapter on Windows
            # Port channel 1 is standard for RFCOMM virtual serial profiles
            port = 1
            server_sock.bind(("00:00:00:00:00:00", port)) 
            server_sock.listen(1)
            
            print(f"\n[BLUETOOTH] RFCOMM Server initialized on channel {port}")
            print("[BLUETOOTH] Ready for pairing. Connect your smartphone companion app...")
            break # Successful bond, exit startup loop
        except Exception as e:
            print(f"[BLUETOOTH] Failed to initialize socket: {e}")
            print("Ensure Bluetooth is enabled in Windows settings. Retrying in 10 seconds...")
            time.sleep(10)

    while True:
        try:
            print("\n[BLUETOOTH] Awaiting incoming smartphone connection...")
            client_socket, client_address = server_sock.accept()
            print(f"[BLUETOOTH] Connection established with mobile device: {client_address}")
            client_connected = True
            
            # Send initial greeting/handshake packet
            handshake = {
                "type": "handshake",
                "device": "Snapdragon_X_Elite_EV_Guardian",
                "timestamp": int(time.time() * 1000)
            }
            send_to_bluetooth(handshake)
            
            # Keep socket alive and monitor for clean client disconnects
            while client_connected:
                # Read dummy packet to verify connection state
                client_socket.settimeout(5.0)
                try:
                    data = client_socket.recv(1024)
                    if not data:
                        break  # Clean close
                except socket.timeout:
                    continue  # Await telemetry updates
                except Exception:
                    break
                    
            print("[BLUETOOTH] Client connection closed.")
            client_connected = False
            if client_socket:
                client_socket.close()
                client_socket = None
        except Exception as e:
            print(f"[BLUETOOTH] Connection loop encounter: {e}")
            client_connected = False
            time.sleep(1)

if __name__ == "__main__":
    # Start MQTT subscription thread
    mqtt_thread = threading.Thread(target=run_mqtt, daemon=True)
    mqtt_thread.start()
    
    # Run Bluetooth Server (blocking)
    run_bluetooth_server()
