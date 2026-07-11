import os
import sys
import time
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, accuracy_score

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fault_classifier.inference import FaultClassificationEngine
from fault_classifier.config import INPUT_FEATURES, PRIMARY_CLASSES
from fault_classifier.feature_engineering import compute_physical_features

print("Loading test telemetry dataset...")
csv_path = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
df = pd.read_csv(csv_path)
print(f"Dataset loaded. Total rows: {len(df)}")

print("Initializing FaultClassificationEngine components...")
engine = FaultClassificationEngine()
seq_len = engine.sequence_length
print(f"Engine ready. Sequence length = {seq_len}")

# Let's compute physical features for the LSTM classifier on the entire dataset at once.
# This avoids doing it repeatedly on small dataframes in a loop!
print("Pre-computing engineered features for the entire dataset...")
df_feat = compute_physical_features(df).fillna(0.0)
scaled_features = engine.scaler.transform(df_feat[INPUT_FEATURES].values).astype(np.float32)
print("Feature scaling complete.")

predictions = []
pipeline_predictions = []
ground_truths = []

# Prepare lists for tracking trust and prediction outputs
overall_trusts = []
anomalous_sensors_list = []

start_time = time.time()

print("Running fast sequential inference...")
# Loop through each row to simulate streaming data
for i in range(len(df)):
    row = df.iloc[i]
    row_dict = {
        "Cell1": float(row["Cell1"]),
        "Cell2": float(row["Cell2"]),
        "Cell3": float(row["Cell3"]),
        "Cell4": float(row["Cell4"]),
        "T1": float(row["T1"]),
        "T2": float(row["T2"]),
        "Current": float(row["Current"]),
        "CO_PPM": float(row["CO_PPM"]),
        "Vib_RMS": float(row["Vib_RMS"]),
        "Vib_Peak": float(row["Vib_Peak"]),
        "Vib_Freq": float(row["Vib_Freq"]),
        "SoC": float(row["SoC"]),
        "Status": str(row["Status"])
    }
    
    # 1. Run Sensor Trust Engine on the row (this updates its sliding window and trust history sequentially!)
    trust_result = engine.trust_engine.diagnose_row(row_dict)
    overall_trust = trust_result['overall_trust']
    overall_trusts.append(overall_trust)
    anomalous_sensors_list.append(trust_result['anomalous_sensors'])
    
    # 2. Run the LSTM classifier if we have warmed up
    if i < seq_len - 1:
        # Warm-up phase prediction (default normal)
        pred_label = "NORMAL"
        raw_pred_class = "NORMAL"
        probs = np.zeros(len(PRIMARY_CLASSES))
        probs[PRIMARY_CLASSES.index("NORMAL")] = 1.0
    else:
        # Get scaled features for the window [i - seq_len + 1 to i]
        window_scaled = scaled_features[i - seq_len + 1 : i + 1]
        onnx_input = np.expand_dims(window_scaled, axis=0) # shape (1, seq_len, num_features)
        
        # Run ONNX Runtime
        ort_inputs = {engine.ort_session.get_inputs()[0].name: onnx_input}
        ort_outs = engine.ort_session.run(None, ort_inputs)
        logits = ort_outs[0][0]
        
        # Softmax
        e_x = np.exp(logits - np.max(logits))
        probs = e_x / e_x.sum(axis=-1)
        raw_pred_idx = int(np.argmax(probs))
        raw_pred_class = engine.encoder.inverse_transform([raw_pred_idx])[0]
        
    # 3. Maintain history queues of size 3 for smoothing (Strategy C)
    engine.prob_history.append(probs)
    if len(engine.prob_history) > 3:
        engine.prob_history.pop(0)
        
    if len(engine.prob_history) < 3:
        final_pred_class = raw_pred_class
        final_prob = float(probs[PRIMARY_CLASSES.index(raw_pred_class)]) if raw_pred_class in PRIMARY_CLASSES else 1.0
    else:
        avg_probs = np.mean(engine.prob_history, axis=0)
        final_pred_idx = int(np.argmax(avg_probs))
        final_pred_class = engine.encoder.inverse_transform([final_pred_idx])[0]
        final_prob = float(avg_probs[final_pred_idx])
        
    # 4. Integrate Sensor Trust overrides
    predictions.append(final_pred_class) # Pure LSTM prediction
    
    # Trust Override prediction
    pipeline_pred = "SENSOR_FAULT" if overall_trust < engine.trust_threshold else final_pred_class
    pipeline_predictions.append(pipeline_pred)
    
    ground_truths.append(str(row["Fault_Name"]))
    
    if i > 0 and i % 500 == 0:
        elapsed = time.time() - start_time
        print(f"  Processed {i}/{len(df)} rows... (Elapsed: {elapsed:.2f}s, speed: {i/elapsed:.1f} rows/sec)", flush=True)

total_time = time.time() - start_time
print(f"\nProcessing complete in {total_time:.2f}s.")

# Filter out the warm-up period (first seq_len rows) for clean evaluation
eval_preds = predictions[seq_len:]
eval_pipeline_preds = pipeline_predictions[seq_len:]
eval_gts = ground_truths[seq_len:]

# Get list of all unique classes present in evaluation set
all_classes = sorted(list(set(eval_gts)))

# Calculate accuracy
accuracy_lstm = accuracy_score(eval_gts, eval_preds)
accuracy_pipeline = accuracy_score(eval_gts, eval_pipeline_preds)

print("\n========================================================")
print("  EVALUATION 1: PURE LSTM BATTERY FAULT CLASSIFIER")
print("========================================================")
print(f"Accuracy: {accuracy_lstm * 100:.2f}%")

# Generate classification report
report_lstm = classification_report(eval_gts, eval_preds, labels=all_classes, output_dict=True, zero_division=0)
report_lstm_df = pd.DataFrame(report_lstm).transpose()
print("\nClassification Report (LSTM):")
print(report_lstm_df.to_string())

print("\nFault-wise Accuracy / Success Rate (LSTM):")
for cls in all_classes:
    idx_cls = [i for i, gt in enumerate(eval_gts) if gt == cls]
    if len(idx_cls) == 0:
        continue
    correct = sum(1 for i in idx_cls if eval_preds[i] == cls)
    success_rate = (correct / len(idx_cls)) * 100
    print(f"  {cls:25s} | Samples: {len(idx_cls):4d} | Detected: {correct:4d} | Success Rate: {success_rate:6.2f}%")

print("\n========================================================")
print("  EVALUATION 2: END-TO-END PIPELINE (WITH SENSOR TRUST)")
print("========================================================")
print(f"Accuracy: {accuracy_pipeline * 100:.2f}%")

report_pipe = classification_report(eval_gts, eval_pipeline_preds, labels=all_classes + ["SENSOR_FAULT"], output_dict=True, zero_division=0)
report_pipe_df = pd.DataFrame(report_pipe).transpose()
print("\nClassification Report (Pipeline):")
print(report_pipe_df.to_string())

print("\nFault-wise Accuracy / Success Rate (Pipeline):")
for cls in all_classes:
    idx_cls = [i for i, gt in enumerate(eval_gts) if gt == cls]
    if len(idx_cls) == 0:
        continue
    correct = sum(1 for i in idx_cls if eval_pipeline_preds[i] == cls)
    success_rate = (correct / len(idx_cls)) * 100
    print(f"  {cls:25s} | Samples: {len(idx_cls):4d} | Detected: {correct:4d} | Success Rate: {success_rate:6.2f}%")

# Save detailed results to a CSV in the temp folder so it can be viewed or used if needed
results_table = []
for cls in all_classes:
    idx_cls = [i for i, gt in enumerate(eval_gts) if gt == cls]
    correct_lstm = sum(1 for i in idx_cls if eval_preds[i] == cls)
    correct_pipe = sum(1 for i in idx_cls if eval_pipeline_preds[i] == cls)
    results_table.append({
        "Fault Type": cls,
        "Samples": len(idx_cls),
        "LSTM Success Rate (%)": round((correct_lstm / len(idx_cls)) * 100, 2),
        "Pipeline Success Rate (%)": round((correct_pipe / len(idx_cls)) * 100, 2)
    })
pd.DataFrame(results_table).to_csv(r"c:\ev vechile\tmp\evaluation_results.csv", index=False)
