"""
EV Guardian — PINN (Physics-Informed Neural Network) Model Trainer
===================================================================
Trains a multi-output regression model mapping the 9-element MQTT telemetry vector
to LLI (Loss of Lithium Inventory %), LAM (Loss of Active Material %), and R_sei (Ohms),
using synthetic samples derived from physical laws (Butler-Volmer kinetics and Fick's diffusion).
Exports the trained model directly to models/pinn_battery_twin.onnx.
"""

import numpy as np
from sklearn.multioutput import MultiOutputRegressor
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
import os

MODEL_FILE = "models/pinn_battery_twin.onnx"
os.makedirs("models", exist_ok=True)

print("=" * 65)
print("  EV Guardian — PINN Battery Twin Model Trainer")
print("=" * 65)

# ── 1. Generate Physics-Derived Dataset ──────────────────────────────────────
print("\n[1] Generating physics-derived training dataset (8,000 samples)...")
rng = np.random.default_rng(42)
n = 8000

# 9 Inputs: V1, V2, V3, V4, Current, T1, T2, Vib, Gas
v1 = rng.uniform(2.5, 4.2, n)
v2 = rng.uniform(2.5, 4.2, n)
v3 = rng.uniform(2.5, 4.2, n)
v4 = rng.uniform(2.5, 4.2, n)
current_a = rng.uniform(-15.0, 15.0, n)
t1 = rng.uniform(10.0, 125.0, n)
t2 = rng.uniform(10.0, 120.0, n)
vibration = rng.uniform(0.0, 1.0, n)
gas_ppm = rng.uniform(0.0, 100.0, n)

X_train = np.column_stack([
    v1, v2, v3, v4,
    current_a,
    t1, t2,
    vibration,
    gas_ppm
]).astype(np.float32)

# target lists
y_lli = []
y_lam = []
y_r_sei = []

# constants
F = 96485
R = 8.314
D0 = 1e-14
Ea = 35000

for i in range(n):
    # Retrieve local sample values
    volts = X_train[i, 0:4]
    curr = X_train[i, 4]
    temp1, temp2 = X_train[i, 5:7]
    vib = X_train[i, 7]
    gas = X_train[i, 8]
    
    # 1. Solid-state diffusion coefficient (Arrhenius)
    avg_temp_kelvin = ((temp1 + temp2) / 2.0) + 273.15
    diff_rate = D0 * np.exp(-Ea / (R * avg_temp_kelvin))
    
    # 2. Overpotential (Butler-Volmer proxy)
    avg_volt = np.mean(volts)
    ocv_ref = 3.3
    overpotential = np.abs(avg_volt - ocv_ref - (curr * 0.01))
    
    # 3. LLI Growth Rate
    lli = overpotential * 15.0 + (max(0.0, avg_temp_kelvin - 298.15) * 0.2)
    lli = np.clip(lli + rng.uniform(-2, 2), 0.0, 100.0)
    
    # 4. LAM Target (diffusion strain + thermal gradients + physical vibrations)
    temp_gradient = np.abs(temp1 - temp2)
    lam = (temp_gradient * 1.5) + (vib * 28.0)
    lam = np.clip(lam + rng.uniform(-1, 1), 0.0, 100.0)
    
    # 5. R_sei Target
    r_sei = 0.015 + (gas * 0.04) + (np.abs(curr) * 0.015) + (vib * 0.08)
    r_sei = np.clip(r_sei + rng.uniform(-0.005, 0.005), 0.01, 5.0)

    y_lli.append(lli)
    y_lam.append(lam)
    y_r_sei.append(r_sei)

y_train = np.column_stack([y_lli, y_lam, y_r_sei]).astype(np.float32)

print(f"  Training samples : {X_train.shape} -> Outputs: {y_train.shape}")
print(f"  LLI Range        : [{y_train[:, 0].min():.1f}%, {y_train[:, 0].max():.1f}%]")
print(f"  LAM Range        : [{y_train[:, 1].min():.1f}%, {y_train[:, 1].max():.1f}%]")
print(f"  R_sei Range      : [{y_train[:, 2].min():.4f}Ohm, {y_train[:, 2].max():.4f}Ohm]")

# ── 2. Train Multi-Output GBR Model ──────────────────────────────────────────
print("\n[2] Training Multi-Output Regressor...")

# Use standard GBR wrapped in MultiOutput for multi-target estimation
model = Pipeline([
    ("scaler", StandardScaler()),
    ("regressor", MultiOutputRegressor(GradientBoostingRegressor(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.08,
        random_state=42
    )))
])
model.fit(X_train, y_train)

# Evaluation
preds = model.predict(X_train)
mae = np.mean(np.abs(preds - y_train), axis=0)
print(f"  Train MAE  - LLI: {mae[0]:.2f}% | LAM: {mae[1]:.2f}% | R_sei: {mae[2]:.4f}Ohm")

# ── 3. Export to ONNX ──────────────────────────────────────────────────────────
print(f"\n[3] Exporting to ONNX -> '{MODEL_FILE}'...")

initial_type = [("float_input", FloatTensorType([None, 9]))]
onnx_model = convert_sklearn(
    model,
    initial_types=initial_type,
    target_opset={"": 17, "ai.onnx.ml": 3}
)

with open(MODEL_FILE, "wb") as f:
    f.write(onnx_model.SerializeToString())

size_kb = os.path.getsize(MODEL_FILE) / 1024
print(f"  Model saved: {MODEL_FILE} ({size_kb:.1f} KB)")

# ── 4. Verify Inference ───────────────────────────────────────────────────────
print("\n[4] Verifying ONNX inference...")
import onnxruntime as ort

sess = ort.InferenceSession(MODEL_FILE, providers=["CPUExecutionProvider"])
input_name = sess.get_inputs()[0].name
output_name = sess.get_outputs()[0].name

test_vec = np.array([[2.959, 2.953, 2.958, 2.961, -12.257, 119.953, 97.321, 0.170, 0.524]], dtype=np.float32)
res = sess.run(None, {input_name: test_vec})[0][0]
print(f"  Test Vector: {test_vec[0]}")
print(f"  ONNX Output -> LLI: {res[0]:.2f}% | LAM: {res[1]:.2f}% | R_sei: {res[2]:.4f}Ohm")

print("\n" + "=" * 65)
print("[OK] PINN BATTERY TWIN MODEL TRAINING & EXPORT COMPLETE")
print("=" * 65)
