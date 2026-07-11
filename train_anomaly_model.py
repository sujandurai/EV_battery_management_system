"""
EV Guardian - ONNX Anomaly Detection Model Trainer
====================================================
Train an Isolation Forest on synthetic healthy EV telemetry,
then export it to ONNX format for use in the real-time backend.

Input  : 1x8 float32 vector
         [c1_v, c2_v, c3_v, c4_v, current_a, max_temp_c, vibration_g, gas_ppm]

Output : anomaly score (float32)
         -1 = anomaly | 1 = normal (IsolationForest convention)
"""

import numpy as np
from sklearn.ensemble import IsolationForest
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
import os

MODEL_FILE = "anomaly_model.onnx"

# ── 1. Generate Synthetic Training Data (Healthy Baseline) ──────────────────
print("Generating synthetic healthy telemetry training data...")

rng = np.random.default_rng(42)
n_samples = 5000

# Healthy ranges (covers real hardware values across operational states)
base_v     = rng.uniform(3.25, 4.20, n_samples)
c1_v       = np.clip(base_v + rng.uniform(-0.03, 0.03, n_samples), 2.5, 4.25)
c2_v       = np.clip(base_v + rng.uniform(-0.03, 0.03, n_samples), 2.5, 4.25)
c3_v       = np.clip(base_v + rng.uniform(-0.03, 0.03, n_samples), 2.5, 4.25)
c4_v       = np.clip(base_v + rng.uniform(-0.03, 0.03, n_samples), 2.5, 4.25)

current_a  = rng.uniform(-10.0, 10.0, n_samples) # normal charging/discharging current
max_temp_c = rng.uniform(20.0, 45.0, n_samples)  # room temperature to moderate load
vibration  = rng.uniform(0.00, 0.20, n_samples)  # normal vibration and sensor noise
gas_ppm    = rng.uniform(0.0, 30.0, n_samples)   # normal background CO levels

X_train = np.column_stack([
    c1_v, c2_v, c3_v, c4_v,
    current_a, max_temp_c, vibration, gas_ppm
]).astype(np.float32)

print(f"  Training samples shape: {X_train.shape}")
print(f"  Healthy voltage range: [{c1_v.min():.3f}, {c1_v.max():.3f}]V")
print(f"  Healthy temp range   : [{max_temp_c.min():.1f}, {max_temp_c.max():.1f}]°C")

# ── 2. Train Isolation Forest ────────────────────────────────────────────────
print("\nTraining Isolation Forest (contamination=0.05)...")

model = IsolationForest(
    n_estimators=100,
    contamination=0.05,   # expect up to 5% anomalies in production
    max_samples="auto",
    random_state=42,
    n_jobs=-1
)
model.fit(X_train)

# Quick sanity check on training data
preds = model.predict(X_train)
normal_pct = (preds == 1).sum() / len(preds) * 100
print(f"  Training  normal prediction rate: {normal_pct:.1f}% (expected ~95%)")

# Sanity check on fault samples
fault_samples = np.array([
    [3.81, 3.82, 1.25, 3.80, -12.5, 115.2, 0.08, 12.0],  # Cell 3 wire disconnect + thermal
    [3.79, 3.80, 1.20, 3.81, -12.0, 118.0, 0.09, 14.0],  # Variation of above fault
    [3.83, 3.82, 0.95, 3.80, -13.0, 120.0, 0.10, 15.0],  # Severe fault
], dtype=np.float32)

fault_preds = model.predict(fault_samples)
fault_scores = model.decision_function(fault_samples)
print(f"\n  Fault sample predictions: {fault_preds}  (expected -1 = anomaly)")
print(f"  Fault decision scores   : {np.round(fault_scores, 4)}")

# ── 3. Export to ONNX ────────────────────────────────────────────────────────
print(f"\nExporting model to ONNX format -> '{MODEL_FILE}'...")

initial_type = [("float_input", FloatTensorType([None, 8]))]
onnx_model = convert_sklearn(
    model,
    initial_types=initial_type,
    target_opset={"": 17, "ai.onnx.ml": 3}
)

with open(MODEL_FILE, "wb") as f:
    f.write(onnx_model.SerializeToString())

size_kb = os.path.getsize(MODEL_FILE) / 1024
print(f"  ONNX model saved: {MODEL_FILE}  ({size_kb:.1f} KB)")

# ── 4. Verify ONNX Inference ─────────────────────────────────────────────────
print("\nVerifying ONNX inference with onnxruntime...")
import onnxruntime as ort

sess = ort.InferenceSession(MODEL_FILE, providers=["CPUExecutionProvider"])
input_name = sess.get_inputs()[0].name
label_name = sess.get_outputs()[0].name
score_name = sess.get_outputs()[1].name

print(f"  Session inputs : {input_name}  shape={sess.get_inputs()[0].shape}")
print(f"  Session outputs: {label_name}, {score_name}")

# Test healthy sample
healthy = np.array([[3.82, 3.81, 3.80, 3.82, -1.5, 34.2, 0.03, 5.0]], dtype=np.float32)
ort_inputs = {input_name: healthy}
label, score_map = sess.run(None, ort_inputs)
# score_map is a list of dicts: [{1: prob_normal, -1: prob_anomaly}, ...]
score_val = score_map[0].get(1, score_map[0].get(b'1', 0.0)) if isinstance(score_map[0], dict) else float(score_map[0])
print(f"\n  [HEALTHY] Label={label[0]}  Score={score_val}")
assert label[0] == 1, "Healthy sample should be predicted as NORMAL!"

# Test fault sample
fault = np.array([[3.81, 3.82, 1.25, 3.80, -12.5, 115.2, 0.08, 12.0]], dtype=np.float32)
ort_inputs = {input_name: fault}
label, score_map = sess.run(None, ort_inputs)
score_val = score_map[0].get(-1, score_map[0].get(b'-1', 0.0)) if isinstance(score_map[0], dict) else float(score_map[0])
print(f"  [FAULT  ] Label={label[0]}  Score={score_val}")
assert label[0] == -1, "Fault sample should be predicted as ANOMALY!"

print("\n" + "="*60)
print("[OK] ONNX MODEL TRAINING & VERIFICATION COMPLETE")
print("="*60)
print(f"  Model file   : {os.path.abspath(MODEL_FILE)}")
print(f"  Input shape  : [batch, 8]  dtype=float32")
print(f"  Output[0]    : label  (1=normal, -1=anomaly)")
print(f"  Output[1]    : probability map dict")
print("\nNext step -> Integrate into backend.py for real-time inference")
