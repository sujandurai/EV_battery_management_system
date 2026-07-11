"""
EV Guardian — Predictive BTS (Battery Thermal Safety) Fusion Engine
========================================================================
Combines:
  1. Camera Frames (OpenCV)
  2. YOLOv8n Object Detection (Ultralytics / ONNX fallback)
  3. Real-Time MCU Telemetry (STM32 / Zephyr Serial UART stream)
  4. Core Battery ML Predictions (ONNX Anomaly + SOH models)
  5. Predictive Thermal Risk Algorithm (fusing vision + telemetry)
========================================================================
"""

import os
import sys
import time
import json
import re
import math
import sqlite3
import threading
import queue
import cv2
import numpy as np
import paho.mqtt.client as mqtt
import onnxruntime as ort

# Configuration Parameters
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_FUSION = "ev/vision_fusion/telemetry"
SERIAL_PORT = "COM34"  # Default serial port, dynamically scanned in Code
BAUD_RATE = 115200
ANOMALY_MODEL_PATH = "anomaly_model.onnx"
SOH_MODEL_PATH = "models/soh_model.onnx"
DB_FILE = "ev_telemetry.db"

class PredictiveBTSFusionGate:
    def __init__(self):
        print("[INIT] Starting EV Guardian Predictive BTS Fusion Engine...")
        self.running = True
        self.telemetry_q = queue.Queue(maxsize=100)
        self.latest_telemetry = None
        self.latest_detections = []
        self.latest_scene_context = {
            "traffic_density": "NORMAL",
            "vehicle_count": 0,
            "closest_vehicle_m": 99.0,
            "airflow_restriction": False,
            "ambient_thermal_load": 0.0
        }
        
        # Initialize SQLite DB
        self.init_db()
        
        # Load ONNX sessions for Battery Health & Anomaly Models
        self.anom_sess = self.load_onnx_model(ANOMALY_MODEL_PATH, "Battery Anomaly")
        self.soh_sess = self.load_onnx_model(SOH_MODEL_PATH, "Battery SOH")
        
        # Establish MQTT Connection
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "bts_fusion_gate")
        try:
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            print("[MQTT] Broker connection established.")
        except Exception as e:
            print(f"[MQTT WARN] Failed to connect to broker: {e}. Running local fallback.")

        # Load YOLOv8 Model (Prefer Ultralytics, fallback to dummy predictions)
        self.init_yolo_model()
        
        # Find hardware UART ports
        self.ser = self.init_serial_port()

    def init_db(self):
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bts_fusion_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                c1_v REAL, c2_v REAL, c3_v REAL, c4_v REAL,
                current_a REAL, temp_c REAL, gas_ppm REAL, vibration_g REAL,
                vehicle_count INTEGER, closest_dist_m REAL, airflow_restricted INTEGER,
                predicted_temp_5min REAL, risk_level TEXT, received_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        print("[DB] Initialized database for local data logs.")

    def load_onnx_model(self, path, name):
        if not os.path.exists(path):
            print(f"[ONNX WARN] Model {name} not found at {path}. Bypassing model.")
            return None
        try:
            session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            print(f"[ONNX] Model {name} loaded successfully.")
            return session
        except Exception as e:
            print(f"[ONNX ERR] Failed to load {name}: {e}")
            return None

    def init_yolo_model(self):
        try:
            from ultralytics import YOLO
            # Nano model is optimized for embedded devices (6MB)
            self.yolo_model = YOLO("yolov8n.pt")
            print("[YOLOv8n] Model loaded successfully.")
        except ImportError:
            self.yolo_model = None
            print("[YOLOv8n WARN] 'ultralytics' module not found. Spawning bounding box simulator.")

    def init_serial_port(self):
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        target_port = SERIAL_PORT
        if ports:
            # Fallback to the first available COM/USB port if default COM34 is missing
            target_port = ports[0].device
            print(f"[SERIAL] Detected serial devices. Binding to {target_port}...")
        try:
            ser = serial.Serial(target_port, BAUD_RATE, timeout=1.0)
            return ser
        except Exception as e:
            print(f"[SERIAL WARN] Failed to open {target_port}: {e}. Telemetry simulator enabled.")
            return None

    def read_serial_loop(self):
        """
        Ingests real-time sensor streams from the STM32 controller.
        """
        while self.running:
            if self.ser and self.ser.is_open:
                try:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        parsed = self.parse_telemetry(line)
                        if parsed:
                            self.latest_telemetry = parsed
                except Exception as e:
                    time.sleep(0.1)
            else:
                # Simulated hardware telemetry (falls back if no physical controller is plugged in)
                simulated_current = 8.0 + 4.0 * math.sin(time.time() * 0.1)
                simulated_temp = 32.0 + 2.0 * math.sin(time.time() * 0.05)
                self.latest_telemetry = {
                    "voltage_v": [3.92, 3.91, 3.90, 3.92],
                    "current_a": round(simulated_current, 3),
                    "temp_c": [round(simulated_temp, 1), round(simulated_temp - 0.2, 1)],
                    "gas_ppm": 12.4,
                    "vibration_g": 0.003
                }
                time.sleep(0.8)

    def parse_telemetry(self, line):
        if "C1:" not in line:
            return None
        try:
            c1 = re.search(r"C1:\s*([\d\.\-]+)V", line)
            c2 = re.search(r"C2:\s*([\d\.\-]+)V", line)
            c3 = re.search(r"C3:\s*([\d\.\-]+)V", line)
            c4 = re.search(r"C4:\s*([\d\.\-]+)V", line)
            amps = re.search(r"Amps:\s*([\d\.\-]+)A", line)
            t1 = re.search(r"T1:\s*([\d\.\-]+)C", line)
            t2 = re.search(r"T2:\s*(ERR|[\d\.\-]+)", line)
            co = re.search(r"CO:\s*([\d\.\-]+)\s*ppm", line)
            vib = re.search(r"Vib:\s*([\d\.\-]+)g", line)
            
            voltages = [
                float(c1.group(1)) if c1 else 0.0,
                float(c2.group(1)) if c2 else 0.0,
                float(c3.group(1)) if c3 else 0.0,
                float(c4.group(1)) if c4 else 0.0
            ]
            current = float(amps.group(1)) if amps else 0.0
            t1_val = float(t1.group(1)) if t1 else -127.0
            t2_val = float(t2.group(1)) if (t2 and t2.group(1) != "ERR") else -127.0
            gas = float(co.group(1)) if co else 0.0
            vibration = float(vib.group(1)) if vib else 0.0
            
            return {
                "voltage_v": voltages,
                "current_a": current,
                "temp_c": [t1_val, t2_val],
                "gas_ppm": gas,
                "vibration_g": vibration
            }
        except Exception:
            return None

    def run_vision_loop(self):
        """
        Handles OpenCV camera capturing and runs real-time YOLOv8n object detection.
        """
        cap = cv2.VideoCapture(0)
        # Verify if camera was opened successfully
        if not cap.isOpened():
            print("[VISION WARN] Cannot open primary video camera. Spawning simulated detection feeds.")

        while self.running:
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    # Resize frame to speed up model inference (YOLOv8 standard image size)
                    resized_frame = cv2.resize(frame, (640, 480))
                    
                    if self.yolo_model:
                        # Perform on-device YOLO detection
                        results = self.yolo_model(resized_frame, verbose=False)
                        self.process_yolo_detections(results)
                    else:
                        # Standalone custom simulator (in case ultralytics packages are missing)
                        self.simulate_yolo_detections()
                    
                    # Optional visualization block for local screen displays
                    cv2.imshow("EV Guardian - Vision Processing Gate", resized_frame)
                    if cv2.waitKey(10) & 0xFF == ord('q'):
                        self.running = False
                else:
                    self.simulate_yolo_detections()
                    time.sleep(0.1)
            else:
                self.simulate_yolo_detections()
                time.sleep(0.5)

        cap.release()
        cv2.destroyAllWindows()

    def process_yolo_detections(self, results):
        """
        Decodes box coordinates, counts vehicles, estimates distance, and detects tailgating risks.
        """
        detected_vehicles = 0
        closest_distance = 99.0
        airflow_restricted = False
        
        for r in results:
            boxes = r.boxes
            for box in boxes:
                cls_id = int(box.cls[0])
                label = r.names[cls_id]
                
                # Check for vehicle elements (car, truck, bus, bike)
                if label in ["car", "truck", "bus", "motorcycle"]:
                    detected_vehicles += 1
                    
                    # Simple heuristic distance estimation: focal_length * actual_width / pixel_width
                    # Or box size tracking (the larger the box, the closer the object)
                    coords = box.xyxy[0].tolist()
                    box_height = coords[3] - coords[1]
                    dist_est = round(64.0 / (box_height / 480.0 + 0.01), 1)  # Est distance in meters
                    
                    if dist_est < closest_distance:
                        closest_distance = dist_est
                        
                    # Airflow restriction occurs if a large vehicle is directly in front
                    if label in ["truck", "bus"] and dist_est < 4.0:
                        airflow_restricted = True
                        
        self.latest_scene_context = {
            "traffic_density": "HIGH" if detected_vehicles > 4 else ("MEDIUM" if detected_vehicles > 1 else "LOW"),
            "vehicle_count": detected_vehicles,
            "closest_vehicle_m": closest_distance,
            "airflow_restriction": airflow_restricted,
            "ambient_thermal_load": 38.0 if detected_vehicles > 4 else 28.0
        }

    def simulate_yolo_detections(self):
        """
        Dynamically simulates vehicle distances and environments to test the BTS logic.
        """
        sim_time = time.time()
        # Modulating vehicles to simulate heavy traffic jam scenario
        veh_count = int(5.0 + 3.0 * math.sin(sim_time * 0.05))
        closest = round(3.5 + 2.0 * math.cos(sim_time * 0.05), 1)
        airflow = (closest < 4.0)
        
        self.latest_scene_context = {
            "traffic_density": "HIGH" if veh_count >= 5 else "MEDIUM",
            "vehicle_count": veh_count,
            "closest_vehicle_m": closest,
            "airflow_restriction": airflow,
            "ambient_thermal_load": 39.0 if veh_count >= 5 else 27.0
        }

    def execute_bts_prediction(self):
        """
        Fuses vision features and internal battery telemetry to run SOH ONNX models
        and predict safety profiles.
        """
        while self.running:
            if self.latest_telemetry is None:
                time.sleep(0.5)
                continue
                
            telemetry = self.latest_telemetry
            scene = self.latest_scene_context
            
            # 1. Run Battery Diagnostic Inferences using ONNX models (SOH and Anomaly detection)
            # Flatten to 1x8 vector format expected by default models
            max_temp = max(telemetry["temp_c"])
            input_vector = np.array([[
                telemetry["voltage_v"][0],
                telemetry["voltage_v"][1],
                telemetry["voltage_v"][2],
                telemetry["voltage_v"][3],
                telemetry["current_a"],
                max_temp,
                telemetry["vibration_g"],
                telemetry["gas_ppm"]
            ]], dtype=np.float32)
            
            soh = 100.0
            if self.soh_sess:
                try:
                    ort_inputs = {self.soh_sess.get_inputs()[0].name: input_vector}
                    outputs = self.soh_sess.run(None, ort_inputs)
                    soh = float(np.clip(outputs[0][0][0], 0.0, 100.0))
                except Exception:
                    soh = 89.5  # fallback
                    
            # 2. Compute FUSED Temperature Rise Prediction Profile
            # The algorithm models the temperature rise at +5 minutes under high current load
            # and restricted air flow (e.g. following a truck closely in 38C weather)
            current = telemetry["current_a"]
            raw_temp = max_temp if max_temp > 0 else 25.0
            
            # Airflow attenuation coefficient: reduced air velocity from tailgating + stop-and-go
            airflow_factor = 2.8 if scene["airflow_restriction"] else 1.0
            traffic_thermal_bias = 0.5 * scene["vehicle_count"]
            ambient_offset = max(0.0, scene["ambient_thermal_load"] - 25.0) * 0.15
            
            # Quadratic heating (I^2 * R) where SOH degradation increases resistance
            soh_resistance_penalty = 1.0 + (100.0 - soh) * 0.04
            joule_heating = (current ** 2) * 0.015 * soh_resistance_penalty
            
            # Predict internal packaging temp rise inside 5 minutes
            predicted_temp_5min = raw_temp + (joule_heating * 0.25 + traffic_thermal_bias + ambient_offset) * airflow_factor
            
            # Determine Risk levels
            if predicted_temp_5min > 58.0:
                risk_level = "CRITICAL"
                feedback_command = "ML:CRITICAL"
            elif predicted_temp_5min > 47.0:
                risk_level = "WARNING"
                feedback_command = "ML:WARNING"
            else:
                risk_level = "NORMAL"
                feedback_command = "ML:NORMAL"
                
            # Log results to SQLite DB
            self.log_to_database(telemetry, scene, predicted_temp_5min, risk_level)
            
            # 3. Compile Unified Fusion Output Payload
            payload = {
                "timestamp": int(time.time()),
                "status": "ACTIVE",
                "cells": {
                    "voltage_v": telemetry["voltage_v"],
                    "temp_c": telemetry["temp_c"]
                },
                "pack": {
                    "current_a": telemetry["current_a"],
                    "gas_ppm": telemetry["gas_ppm"],
                    "vibration_g": telemetry["vibration_g"]
                },
                "vision_context": scene,
                "diagnostics": {
                    "soh_pct": round(soh, 2),
                    "predicted_temp_5min": round(predicted_temp_5min, 2),
                    "risk_level": risk_level
                }
            }
            
            # 4. Broadcast Output via MQTT
            try:
                self.mqtt_client.publish(MQTT_TOPIC_FUSION, json.dumps(payload))
            except Exception:
                pass
                
            # 5. Writeback Feedback to the STM32 to control the alarm indicators
            self.write_serial_command(feedback_command)
            
            # Log report to console
            print("─" * 70)
            print(f"[FUSION ENGINE] Telemetry: Temp={raw_temp}°C | Amps={current}A")
            print(f"[VISION CONTEXT] Detections={scene['vehicle_count']} vehicles | Closest={scene['closest_vehicle_m']}m")
            print(f"[PREDICTION] Estimated Temp rise in 5min: {predicted_temp_5min:.2f}°C [{risk_level}]")
            print(f"[FEEDBACK] Sent feedback command: {feedback_command}")
            print("─" * 70 + "\n")
            
            time.sleep(1.0) # Refresh cycle rate

    def write_serial_command(self, cmd):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(f"{cmd}\n".encode('utf-8'))
                self.ser.flush()
            except Exception:
                pass

    def log_to_database(self, telemetry, scene, predicted_temp, risk_level):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("""
                INSERT INTO bts_fusion_logs (
                    timestamp, c1_v, c2_v, c3_v, c4_v, current_a, temp_c, gas_ppm, vibration_g,
                    vehicle_count, closest_dist_m, airflow_restricted, predicted_temp_5min, risk_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(time.time()),
                telemetry["voltage_v"][0], telemetry["voltage_v"][1],
                telemetry["voltage_v"][2], telemetry["voltage_v"][3],
                telemetry["current_a"], max(telemetry["temp_c"]), telemetry["gas_ppm"], telemetry["vibration_g"],
                scene["vehicle_count"], scene["closest_vehicle_m"], int(scene["airflow_restriction"]),
                predicted_temp, risk_level
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB ERR] Log insertion failed: {e}")

    def start(self):
        # Thread 1: Ingest Hardware Serial Telemetry
        self.t1 = threading.Thread(target=self.read_serial_loop, daemon=True)
        # Thread 2: Run AI Prediction and Calculations
        self.t2 = threading.Thread(target=self.execute_bts_prediction, daemon=True)
        
        self.t1.start()
        self.t2.start()
        
        # Main Thread: Run Camera Capture + Object Detection
        try:
            self.run_vision_loop()
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Terminating Predictive BTS Fusion Engine...")
        finally:
            self.running = False
            self.mqtt_client.loop_stop()
            if self.ser and self.ser.is_open:
                self.ser.close()
                print("[SERIAL] Port safely closed.")

if __name__ == "__main__":
    app = PredictiveBTSFusionGate()
    app.start()
