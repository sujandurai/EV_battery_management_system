import onnxruntime as ort
import numpy as np

print("--- Testing Real Hardware Data on the Trained Models ---")

# Real hardware data vector
# Volts: 4.19, 4.17, 4.16, 4.13
# Current: 0.0
# Temp: 27.5 (max of 27.0, 27.5)
# Vib: 0.133
# Gas: 26.2
real_vec_8 = np.array([[4.19, 4.17, 4.16, 4.13, 0.0, 27.5, 0.133, 26.2]], dtype=np.float32)

print(f"Real Hardware Input Vector: {real_vec_8}")

# 1. Anomaly Model Test
try:
    so = ort.SessionOptions()
    sess = ort.InferenceSession("anomaly_model.onnx", sess_options=so, providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    label, score_map = sess.run(None, {iname: real_vec_8})
    print(f"\n[ANOMALY MODEL]")
    print(f"  Prediction Label: {label[0]} (1=Normal, -1=Anomaly)")
    print(f"  Score Map: {score_map}")
except Exception as e:
    print(f"[ANOMALY MODEL ERROR]: {e}")

# 2. SOH Model Test
try:
    so = ort.SessionOptions()
    sess = ort.InferenceSession("models/soh_model.onnx", sess_options=so, providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    out = sess.run(None, {iname: real_vec_8})[0]
    soh_val = float(np.array(out).flat[0])
    print(f"\n[SOH MODEL]")
    print(f"  Predicted SOH: {soh_val:.2f}%")
except Exception as e:
    print(f"[SOH MODEL ERROR]: {e}")

# 3. PINN Model Test
try:
    # 9-element vector: V1, V2, V3, V4, Current, T1, T2, Vib, Gas
    real_vec_9 = np.array([[4.19, 4.17, 4.16, 4.13, 0.0, 27.0, 27.5, 0.133, 26.2]], dtype=np.float32)
    so = ort.SessionOptions()
    sess = ort.InferenceSession("models/pinn_battery_twin.onnx", sess_options=so, providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    out = sess.run(None, {iname: real_vec_9})[0]
    res = np.array(out).flat
    print(f"\n[PINN MODEL]")
    print(f"  Predicted LLI: {res[0]:.2f}%")
    print(f"  Predicted LAM: {res[1]:.2f}%")
    print(f"  Predicted R_sei: {res[2]:.4f} Ohm")
except Exception as e:
    print(f"[PINN MODEL ERROR]: {e}")
