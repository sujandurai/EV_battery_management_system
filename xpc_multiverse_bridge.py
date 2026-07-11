import os
import sys
import time
import json
import socket
import threading
import queue
import cv2
import paho.mqtt.client as mqtt
import requests

# ============================================================================
# EV GUARDIAN - Snapdragon X PC (XPC) Multiverse Coordinator App
# ============================================================================
# This application implements the core flow:
#   [Arduino Uno Q (Sensors + Trust Engine)] 
#                 | (MQTT)
#                 v
#   [Snapdragon X PC (Inference, Eyegaze/Drowsiness, VLM/LLM)]
#                 | (Bluetooth RFCOMM)
#                 v
#   [OnePlus 15 Smartphone (AR, TTS Voice, Haptic Alerts)]
# ============================================================================

# Configuration parameters
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_TELEMETRY = "ev/sensor/telemetry"
MQTT_TOPIC_TRUST = "ev/analytics/trust_status"
LLM_API_URL = "http://127.0.0.1:8766/diagnose"
BLUETOOTH_PORT = 1 # RFCOMM channel 1

class XPCMultiverseCoordinator:
    def __init__(self):
        print("================================================================")
        print("  EV GUARDIAN — Snapdragon X PC Multiverse Coordinator App")
        print("================================================================")
        self.running = True
        self.telemetry_q = queue.Queue(maxsize=100)
        self.latest_telemetry = None
        self.arduino_trust_score = 98.0  # Initial default trust from STM32
        
        # Driver Monitoring State Variables (Simulated/Webcam inputs)
        self.driver_state = {
            "gaze_focused": True,
            "blink_rate_minute": 14.0,
            "drowsiness_index": 0.0,  # Range [0.0 to 1.0]
            "attention_status": "FOCUSED"
        }
        
        # Bluetooth Server State
        self.bt_connected = False
        self.bt_client_sock = None
        self.bt_server_sock = None

        # Initialize MQTT Local Client
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "xpc_multiverse_coordinator")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[MQTT] Connected. Subscribing to telemetry topics...")
            self.mqtt_client.subscribe(MQTT_TOPIC_TELEMETRY)
            self.mqtt_client.subscribe(MQTT_TOPIC_TRUST)
        else:
            print(f"[MQTT ERR] Connection failed with code {rc}")

    def on_mqtt_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if message.topic == MQTT_TOPIC_TELEMETRY:
                self.latest_telemetry = payload
            elif message.topic == MQTT_TOPIC_TRUST:
                # Extract Sensor Trust Score determined by the STM32 Autoencoder
                # e.g., {"status": "OK", "trust_score": 98.2}
                self.arduino_trust_score = payload.get("trust_score", 98.0)
        except Exception as e:
            pass

    def run_driver_monitoring(self):
        """
        Runs the webcam driver monitoring frame loop.
        Fuses gaze tracking and blink rate calculation.
        """
        print("[VISION] Starting local driver monitoring loop via camera...")
        cap = cv2.VideoCapture(0)
        
        # Load local Haar Cascade for face/eye tracking if available, else run simulator
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

        eye_closed_frames = 0
        last_frame_time = time.time()

        while self.running:
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    faces = face_cascade.detectMultiScale(gray, 1.3, 5)
                    
                    if len(faces) == 0:
                        # Driver has looked away entirely or head is tilted
                        self.driver_state["gaze_focused"] = False
                        self.driver_state["attention_status"] = "DISTRACTED"
                    else:
                        self.driver_state["gaze_focused"] = True
                        
                        # Inspect eyes inside face boundaries
                        for (x, y, w, h) in faces:
                            roi_gray = gray[y:y+h, x:x+w]
                            eyes = eye_cascade.detectMultiScale(roi_gray)
                            
                            if len(eyes) == 0:
                                eye_closed_frames += 1
                            else:
                                eye_closed_frames = max(0, eye_closed_frames - 1)
                        
                        # If eyes closed for >10 consecutive frames (~1.5 seconds), flag drowsiness
                        if eye_closed_frames > 10:
                            self.driver_state["drowsiness_index"] = min(1.0, self.driver_state["drowsiness_index"] + 0.1)
                            self.driver_state["attention_status"] = "DROWSY"
                        else:
                            self.driver_state["drowsiness_index"] = max(0.0, self.driver_state["drowsiness_index"] - 0.05)
                            if self.driver_state["drowsiness_index"] < 0.3:
                                self.driver_state["attention_status"] = "FOCUSED"
                    
                    # Render monitoring screen overlay locally
                    status_color = (0, 255, 0)
                    if self.driver_state["attention_status"] == "DROWSY":
                        status_color = (0, 0, 255)
                    elif self.driver_state["attention_status"] == "DISTRACTED":
                        status_color = (0, 165, 255)

                    cv2.putText(
                        frame, 
                        f"STATUS: {self.driver_state['attention_status']} (Drowsy={self.driver_state['drowsiness_index']:.1f})", 
                        (20, 40), 
                        cv2.FONT_HERSHEY_SIMPLEX, 
                        0.7, 
                        status_color, 
                        2
                    )
                    cv2.imshow("Driver Attention Monitor (Local Hub)", frame)
                    if cv2.waitKey(10) & 0xFF == ord('q'):
                        self.running = False
                else:
                    self.simulate_driver_state()
                    time.sleep(0.1)
            else:
                self.simulate_driver_state()
                time.sleep(0.5)

        cap.release()
        cv2.destroyAllWindows()

    def simulate_driver_state(self):
        """Webcam fallback: simulates gaze drift and drowsiness oscillations"""
        t = time.time()
        # Modulate driver state dynamically every 30 seconds
        cycle = t % 60
        if cycle < 40:
            self.driver_state = {
                "gaze_focused": True,
                "blink_rate_minute": 15.0 + 2.0 * math.sin(t * 0.1) if 'math' in sys.modules else 15.0,
                "drowsiness_index": 0.0,
                "attention_status": "FOCUSED"
            }
        elif cycle < 50:
            self.driver_state = {
                "gaze_focused": False,
                "blink_rate_minute": 8.0,
                "drowsiness_index": 0.1,
                "attention_status": "DISTRACTED"
            }
        else:
            self.driver_state = {
                "gaze_focused": True,
                "blink_rate_minute": 24.0,
                "drowsiness_index": 0.85,
                "attention_status": "DROWSY"
            }

    def run_bluetooth_server(self):
        """Hosts the Bluetooth RFCOMM socket to pair with the Smartphone companion app"""
        while self.running:
            try:
                self.bt_server_sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
                # Bind to the standard default adapter
                self.bt_server_sock.bind(("00:00:00:00:00:00", BLUETOOTH_PORT))
                self.bt_server_sock.listen(1)
                print(f"[BLUETOOTH] Server bound on RFCOMM port {BLUETOOTH_PORT}. Awaiting smartphone pairing...")
                break
            except Exception as e:
                print(f"[BLUETOOTH ERR] Port bind failed: {e}. Retrying in 10 seconds...")
                time.sleep(10)

        while self.running:
            try:
                self.bt_client_sock, client_addr = self.bt_server_sock.accept()
                print(f"[BLUETOOTH] Client connected: {client_addr}")
                self.bt_connected = True
                
                # Send Handshake Greeting confirming pipeline active
                handshake = {
                    "type": "handshake",
                    "device": "Snapdragon_X_Elite_XPC",
                    "timestamp": int(time.time() * 1000)
                }
                self.send_to_mobile(handshake)

                # Keep connection check loop alive
                self.bt_client_sock.settimeout(5.0)
                while self.bt_connected:
                    try:
                        # Scan for heartbeat/null check from phone
                        data = self.bt_client_sock.recv(1024)
                        if not data:
                            break
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            except Exception as e:
                print(f"[BLUETOOTH] Connection dropped: {e}")
            finally:
                self.bt_connected = False
                if self.bt_client_sock:
                    self.bt_client_sock.close()
                print("[BLUETOOTH] Ready for reconnection.")
                time.sleep(1.0)

    def send_to_mobile(self, packet):
        """Transmits JSON buffer terminated with newline over RFCOMM"""
        if self.bt_connected and self.bt_client_sock:
            try:
                payload = json.dumps(packet) + "\n"
                self.bt_client_sock.send(payload.encode("utf-8"))
            except Exception:
                self.bt_connected = False

    def query_local_llm_safety_advice(self, fault_code, driver_condition):
        """
        Sends the coupled vehicle + driver state to the offloaded local diagnostic LLM.
        """
        combined_reason = f"{fault_code} | DRIVER_STATUS: {driver_condition}"
        try:
            url = f"{LLM_API_URL}?reason={requests.utils.quote(combined_reason)}"
            resp = requests.get(url, timeout=5.0)
            if resp.status_code == 200:
                return resp.json().get("diagnosis", "Error pulling active safety advice.")
        except Exception:
            pass

        # Local hardcoded fallback mapping if the local diagnostic server is offline
        if "DROWSY" in driver_condition and "TEMP_HIGH" in fault_code:
            return (
                "[EMERGENCY PILOT ACTION ADVISE]\n"
                "1. Driver drowsiness detected while Battery Cell Overheats.\n"
                "2. HAPTICS: Request OnePlus 15 to trigger continuous motor vibration pulse to wake driver.\n"
                "3. AUDIO: Synthesize emergency stop guide verbally.\n"
                "4. VECHILE Control: Activating high-priority cooling loop and restricting speed."
            )
        return f"Warning: Fault detected: {fault_code}. Proceed to nearest maintenance post cautiously."

    def run_multiverse_sync_loop(self):
        """
        Fuses the STM32 sensor data + local XPC Driver state and broadcasts 
        intelligent outputs to the mobile app.
        """
        print("[COORDINATOR] Running Multiverse Sync Loop...")
        last_relay = 0.0

        while self.running:
            time.sleep(1.0)
            if not self.latest_telemetry:
                continue

            telemetry = self.latest_telemetry
            driver = self.driver_state
            
            # Determine if a warning condition exists
            is_anomaly = telemetry.get("is_anomaly", False)
            sensor_trust = self.arduino_trust_score
            driver_troubled = driver["attention_status"] in ["DROWSY", "DISTRACTED"]
            
            # Determine safety hazard risk level:
            # - CRITICAL: Anomaly + Drowsy driver
            # - WARNING: Anomaly alone, or Drowsy driver alone
            # - NORMAL: Everything nominal
            risk_level = "NORMAL"
            if is_anomaly and driver["attention_status"] == "DROWSY":
                risk_level = "CRITICAL"
            elif is_anomaly or driver_troubled or sensor_trust < 80.0:
                risk_level = "WARNING"

            # Create integrated Multiverse Safety Envelope
            envelope = {
                "type": "telemetry",
                "timestamp": int(time.time() * 1000),
                "voltages": telemetry.get("voltage_v", [0,0,0,0]),
                "temperatures": telemetry.get("temp_c", [0,0]),
                "current": telemetry.get("current_a", 0.0),
                "soh_pct": telemetry.get("soh_pct", 100.0),
                "sensor_trust_pct": sensor_trust,
                "driver_monitoring": {
                    "status": driver["attention_status"],
                    "gaze_focused": driver["gaze_focused"],
                    "drowsiness_index": driver["drowsiness_index"]
                },
                "hazard_assessment": {
                    "is_anomaly": is_anomaly or (risk_level == "CRITICAL"),
                    "risk_level": risk_level,
                    "alert_reason": telemetry.get("alert_reason", "SYSTEM_OK")
                },
                "diagnosis": "System fully stable. Safe lock active."
            }

            # If our hazard state escalates, execute the local LLM query and change envelope type to Alert
            if risk_level in ["WARNING", "CRITICAL"]:
                fault_reason = telemetry.get("alert_reason", "MODEL_ANOMALY")
                if sensor_trust < 80.0:
                    fault_reason = f"SENSOR_TRUST_DEGRADED({sensor_trust:.1f}%)"
                
                # Retrieve fused diagnostic advice
                advice = self.query_local_llm_safety_advice(fault_reason, driver["attention_status"])
                envelope["type"] = "alert"
                envelope["diagnosis"] = advice

            # Broadcast fused envelope to mobile device
            if self.bt_connected:
                self.send_to_mobile(envelope)
                print(f"[COORDINATOR] Broadcasted sync frame [Risk: {risk_level}] to Smartphone.")
            else:
                # Log locally to terminal console
                v = [round(x, 2) for x in telemetry.get("voltage_v", [])]
                print(f"[OK] Telemetry log: V={v} | Driver: {driver['attention_status']} | Risk: {risk_level} (BT offline)")

    def start(self):
        # Thread 1: Start MQTT client loop (daemon)
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.mqtt_thread = threading.Thread(target=self.mqtt_client.loop_forever, daemon=True)
        self.mqtt_thread.start()
        
        # Thread 2: Start Bluetooth RFCOMM server
        self.bt_thread = threading.Thread(target=self.run_bluetooth_server, daemon=True)
        self.bt_thread.start()
        
        # Thread 3: Start Coordinator Sync Loop
        self.sync_thread = threading.Thread(target=self.run_multiverse_sync_loop, daemon=True)
        self.sync_thread.start()

        # Main Thread: Run webcam Driver Gaze Monitoring
        try:
            self.run_driver_monitoring()
        except KeyboardInterrupt:
            print("\n[STOPPING] Terminating XPC Multiverse coordinator...")
        finally:
            self.running = False
            if self.bt_server_sock:
                self.bt_server_sock.close()
            print("[SHUTDOWN] Safely wound down threads.")

if __name__ == "__main__":
    import math # required for simulation metrics
    coordinator = XPCMultiverseCoordinator()
    coordinator.start()
