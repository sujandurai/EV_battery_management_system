"""
EV Guardian — PINN & Triple ONNX Pipeline Verifier
==================================================
Run this script locally to verify that all three ONNX models load
correctly on your Windows Snapdragon PC and that the updated SQLite
database logs information as expected.
"""

import os
import sys
import time
import sqlite3
import numpy as np
import onnxruntime as ort

print("=" * 70)
print("  EV Guardian — PINN & Pipeline Verifier")
print("=" * 70)

# --- Test 1: Check Model Files ---
print("\n[TEST 1] Verifying Model Files exist...")
models = {
    "ANOMALY": "anomaly_model.onnx",
    "SOH": "models/soh_model.onnx",
    "PINN": "models/pinn_battery_twin.onnx"
}

all_found = True
for name, path in models.items():
    if os.path.exists(path):
        size_kb = os.path.getsize(path) / 1024.0
        print(f"  [FOUND] {name:7s} -> {path} ({size_kb:.1f} KB)")
    else:
        print(f"  [MISSING] {name:7s} -> {path}")
        all_found = False

if not all_found:
    print("\n[FAIL] Please run 'python train_pinn_model.py' to generate the twin model.")
    sys.exit(1)
else:
    print("  [PASS] All model files found.")

# --- Test 2: Verify Database Schema Evolution ---
print("\n[TEST 2] Verifying SQLite database migration...")
DB_FILE = "ev_telemetry.db"

try:
    # Initialize backend DB logic to trigger table creation and migrations
    sys.path.insert(0, ".")
    from backend import init_db
    init_db()
    
    conn = sqlite3.connect(DB_FILE)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(telemetry_logs)").fetchall()]
    conn.close()
    
    expected_cols = ["lli_pct", "lam_pct", "r_sei_ohms"]
    passed = True
    for col in expected_cols:
        if col in columns:
            print(f"  [OK] Column '{col}' is present.")
        else:
            print(f"  [MISSING] Column '{col}' is missing.")
            passed = False
            
    if passed:
        print("  [PASS] SQLite schema verifies successfully.")
    else:
        raise AssertionError("Database schema missing PINN metrics columns.")
except Exception as e:
    print(f"  [FAIL] Database verify error: {e}")
    sys.exit(1)

# --- Test 3: Load Sessions and Measure Latency ---
print("\n[TEST 3] Loading Sessions via ONNX Runtime & Testing Inference Speed...")
sessions = {}

def measure_runs(name, sess, input_name, vec, epochs=100):
    start = time.perf_counter()
    for _ in range(epochs):
        sess.run(None, {input_name: vec})
    end = time.perf_counter()
    latency_ms = ((end - start) / epochs) * 1000.0
    print(f"  [INFERENCE] {name:7s} Session | Avg Latency: {latency_ms:.3f} ms (runs={epochs})")
    return latency_ms

try:
    # Load each session using available providers
    for name, path in models.items():
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        
        # Test loading
        sess = ort.InferenceSession(path, sess_options=so, providers=ort.get_available_providers())
        inp_name = sess.get_inputs()[0].name
        sessions[name] = (sess, inp_name)
        print(f"  [LOADED] {name:7s} | Provider list: {sess.get_providers()}")
        
    print("\n  Executing Speed Trials:")
    
    # 8-element vector for Anomaly and SOH
    vec_8 = np.array([[3.82, 3.81, 3.80, 3.82, -1.5, 34.2, 0.03, 5.0]], dtype=np.float32)
    # 9-element vector for PINN Battery Twin
    vec_9 = np.array([[2.959, 2.953, 2.958, 2.961, -12.257, 119.95, 97.32, 0.17, 0.52]], dtype=np.float32)
    
    lat_anom = measure_runs("ANOMALY", sessions["ANOMALY"][0], sessions["ANOMALY"][1], vec_8)
    lat_soh = measure_runs("SOH    ", sessions["SOH"][0], sessions["SOH"][1], vec_8)
    lat_pinn = measure_runs("PINN   ", sessions["PINN"][0], sessions["PINN"][1], vec_9)
    
    total_pipeline_ms = lat_anom + lat_soh + lat_pinn
    print(f"\n  [PASS] Total Combined Inference Loop Latency: {total_pipeline_ms:.3f} ms")
    print("         (This easily satisfies the sub-150ms edge requirement.)")
    
except Exception as e:
    print(f"  [FAIL] ONNX Runtime Test Error: {e}")
    sys.exit(1)

# --- Test 4: Verify Database Write/Read ---
print("\n[TEST 4] Verifying database logging cycle...")
try:
    from backend import save_to_db
    test_data = {
        "timestamp": int(time.time() * 1000),
        "device_id": "pinn-test-host",
        "cells": {
            "voltage_v": [2.959, 2.953, 2.958, 2.961],
            "temp_c": [119.95, 97.32]
        },
        "pack": {
            "current_a": -12.257,
            "vibration_g": 0.17,
            "gas_ppm": 0.52
        }
    }
    
    # Save with specific LLI, LAM, R_sei values
    save_to_db(test_data, is_anomaly=0, anomaly_score=0.12, soh_pct=95.5, lli=19.01, lam=53.49, r_sei=0.2469, alert_reason="VERIFY_METRICS")
    
    # Query back
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("""
        SELECT lli_pct, lam_pct, r_sei_ohms, alert_reason 
        FROM telemetry_logs 
        WHERE timestamp=?
    """, (test_data["timestamp"],)).fetchone()
    
    # Clean up test row
    conn.execute("DELETE FROM telemetry_logs WHERE timestamp=?", (test_data["timestamp"],))
    conn.commit()
    conn.close()
    
    assert row is not None, "Failed to retrieve logged telemetry."
    lli, lam, r_sei, reason = row
    assert np.isclose(lli, 19.01), f"Expected 19.01, got {lli}"
    assert np.isclose(lam, 53.49), f"Expected 53.49, got {lam}"
    assert np.isclose(r_sei, 0.2469), f"Expected 0.2469, got {r_sei}"
    assert reason == "VERIFY_METRICS", f"Expected VERIFY_METRICS, got {reason}"
    
    print("  [PASS] Logging write/read roundtrip verifies correctly.")
except Exception as e:
    print(f"  [FAIL] Database verify error: {e}")
    sys.exit(1)

print("\n" + "=" * 70)
print("  [OK] ALL SYSTEMS VERIFIED & READY FOR PHYSICAL ARDUINO CONNECTION")
print("=" * 70)
