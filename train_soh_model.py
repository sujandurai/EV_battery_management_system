"""
EV Guardian — SOH (State of Health) ONNX Model (Step: Dual Inference)
=======================================================================
Trains a regression model that estimates battery State of Health (%)
from the 1×8 feature vector, then exports to ONNX.

SOH = capacity retention (100% = brand new, <70% = replace).
Simulates aging by correlating:
  - Cell voltage imbalance -> higher imbalance = higher degradation
  - Max temperature -> heat accelerates aging
  - Cumulative micro-charge cycles (encoded via current signature)

Input  : [c1_v, c2_v, c3_v, c4_v, current_a, max_temp_c, vibration_g, gas_ppm]
Output : soh_percent  (float32, 0–100)
"""

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
import os

MODEL_FILE = "models/soh_model.onnx"
os.makedirs("models", exist_ok=True)

print("=" * 65)
print("  EV Guardian — SOH Regression Model Trainer")
print("=" * 65)

# ── 1. Synthetic SOH Training Data ────────────────────────────────────────────
print("\n[1] Generating synthetic SOH training dataset (8,000 samples)...")

rng = np.random.default_rng(7)
n   = 8000

# Simulate a fleet of batteries at various aging stages (20%–100% SOH)
soh_true = rng.uniform(20.0, 100.0, n)  # Ground truth SOH %

# Feature engineering: derive plausible sensor readings from SOH
# As SOH decreases:
#   - Cells show higher voltage imbalance (degraded cells drift lower)
#   - Average cell voltage slightly lower (capacity drop shifts resting V)
#   - Higher internal resistance -> higher temps at same current
#   - Gas ppm slightly elevated in degraded packs

imbalance   = (100 - soh_true) / 100 * 0.35 + rng.uniform(0, 0.02, n)
base_v      = rng.uniform(3.25, 4.20, n)  # Voltage range corresponding to full operating window

c1_v = np.clip(base_v + rng.uniform(-0.02, 0.02, n), 2.5, 4.25)
c2_v = np.clip(base_v + rng.uniform(-0.02, 0.02, n), 2.5, 4.25)
c3_v = np.clip(base_v - imbalance + rng.uniform(-0.01, 0.01, n), 2.5, 4.25)  # Weakest cell
c4_v = np.clip(base_v + rng.uniform(-0.02, 0.02, n), 2.5, 4.25)

# Temperature rises with degradation, but ambient fluctuates from 20 to 38C
base_temp   = 20.0 + (100 - soh_true) / 100 * 15.0
max_temp_c  = base_temp + rng.uniform(0, 15, n)

current_a   = rng.uniform(-10.0, 10.0, n)
vibration   = rng.uniform(0.00, 0.20, n)
gas_ppm     = rng.uniform(2.0, 35.0, n)

X_train = np.column_stack([
    c1_v, c2_v, c3_v, c4_v,
    current_a, max_temp_c, vibration, gas_ppm
]).astype(np.float32)

y_train = soh_true.astype(np.float32)

print(f"  Training samples : {X_train.shape}")
print(f"  SOH range        : [{y_train.min():.1f}%, {y_train.max():.1f}%]  mean={y_train.mean():.1f}%")
print(f"  Imbalance range  : [{imbalance.min():.4f}, {imbalance.max():.4f}] V")

# ── 2. Train Gradient Boosting Regressor ──────────────────────────────────────
print("\n[2] Training Gradient Boosting Regressor...")

model = Pipeline([
    ("scaler", StandardScaler()),
    ("gbr", GradientBoostingRegressor(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        min_samples_leaf=10,
        random_state=42
    ))
])
model.fit(X_train, y_train)

# Evaluation
preds_train = model.predict(X_train)
residuals   = np.abs(preds_train - y_train)
print(f"  Train MAE  : {residuals.mean():.2f}%")
print(f"  Train MaxE : {residuals.max():.2f}%")

# Quick sanity checks
test_cases = [
    ([3.84, 3.83, 3.82, 3.83, -1.5, 34.0, 0.03, 5.0],  "Brand new (expected ~95-100%)"),
    ([3.80, 3.79, 3.70, 3.80, -2.0, 39.0, 0.04, 7.5],  "Moderate aging (expected ~70-80%)"),
    ([3.78, 3.76, 3.55, 3.77, -2.5, 44.0, 0.05, 9.0],  "Degraded (expected ~45-60%)"),
    ([3.75, 3.70, 3.40, 3.73, -3.0, 48.0, 0.06, 11.0], "End-of-life (expected ~20-35%)"),
]

print("\n  Sanity checks:")
for features, desc in test_cases:
    x = np.array([features], dtype=np.float32)
    soh_est = float(np.clip(model.predict(x)[0], 0, 100))
    print(f"    {desc}")
    print(f"      -> Estimated SOH: {soh_est:.1f}%")

# ── 3. Export to ONNX ─────────────────────────────────────────────────────────
print(f"\n[3] Exporting to ONNX -> '{MODEL_FILE}'...")

initial_type = [("float_input", FloatTensorType([None, 8]))]
onnx_model   = convert_sklearn(
    model,
    initial_types=initial_type,
    target_opset={"": 17, "ai.onnx.ml": 3}
)

with open(MODEL_FILE, "wb") as f:
    f.write(onnx_model.SerializeToString())

size_kb = os.path.getsize(MODEL_FILE) / 1024
print(f"  Saved: {MODEL_FILE}  ({size_kb:.1f} KB)")

# ── 4. Verify ONNX Inference ──────────────────────────────────────────────────
print("\n[4] Verifying ONNX inference with onnxruntime...")
import onnxruntime as ort

sess       = ort.InferenceSession(MODEL_FILE, providers=["CPUExecutionProvider"])
input_name = sess.get_inputs()[0].name
out_name   = sess.get_outputs()[0].name
print(f"  Input : '{input_name}'  shape={sess.get_inputs()[0].shape}")
print(f"  Output: '{out_name}'   shape={sess.get_outputs()[0].shape}")

for features, desc in test_cases:
    x   = np.array([features], dtype=np.float32)
    soh = float(np.clip(sess.run(None, {input_name: x})[0][0], 0, 100))
    print(f"  {desc[:40]:40s} -> SOH={soh:.1f}%")

print("\n" + "=" * 65)
print("[OK] SOH MODEL TRAINING & ONNX EXPORT COMPLETE")
print("=" * 65)
print(f"  Model file  : {os.path.abspath(MODEL_FILE)}")
print(f"  Input       : [batch, 8]  float32")
print(f"  Output      : [batch, 1]  float32  (SOH %)")
print("\nCopy models/soh_model.onnx to backend root and integrate with backend.py")
