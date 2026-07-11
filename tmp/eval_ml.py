import os
import sys
import time
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, accuracy_score

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fault_classifier.inference import FaultClassificationEngine

print("Loading dataset...")
csv_path = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
df = pd.read_csv(csv_path)
print(f"Dataset loaded. Total rows: {len(df)}")

print("Initializing FaultClassificationEngine...")
engine = FaultClassificationEngine()
print("Engine ready.")

predictions = []
ground_truths = []

# Sequence length
seq_len = engine.sequence_length
print(f"Model Sequence Length (warm-up): {seq_len}")

start_time = time.time()

# Slide over the dataset to perform streaming prediction
for i in range(len(df)):
    if i % 100 == 0:
        elapsed = time.time() - start_time
        print(f"Processing row {i}/{len(df)}... (Elapsed: {elapsed:.2f}s)")
        
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
    
    # Send row to engine
    engine.add_row(row_dict)
    
    # Run prediction
    res = engine.predict()
    
    pred_label = res["prediction"]
    gt_label = str(row["Fault_Name"])
    
    predictions.append(pred_label)
    ground_truths.append(gt_label)

total_time = time.time() - start_time
print(f"Processing complete in {total_time:.2f}s. Generating metrics...")

# Filter out the warm-up period (first seq_len rows) for clean evaluation
eval_preds = predictions[seq_len:]
eval_gts = ground_truths[seq_len:]

# Get list of all unique classes present in evaluation set
all_classes = sorted(list(set(eval_gts + eval_preds)))

# Calculate accuracy
accuracy = accuracy_score(eval_gts, eval_preds)
print(f"\nOverall Pipeline Diagnostic Accuracy: {accuracy * 100:.2f}%")

# Generate classification report
report = classification_report(eval_gts, eval_preds, labels=all_classes, output_dict=True)
report_df = pd.DataFrame(report).transpose()

print("\nClassification Report:")
print(report_df.to_string())

# Group metrics by fault name
print("\nFault-wise Accuracy / Success Rate:")
for cls in all_classes:
    idx_cls = [i for i, gt in enumerate(eval_gts) if gt == cls]
    if len(idx_cls) == 0:
        continue
    correct = sum(1 for i in idx_cls if eval_preds[i] == cls)
    success_rate = (correct / len(idx_cls)) * 100
    print(f"  {cls:25s} | Samples: {len(idx_cls):4d} | Detected: {correct:4d} | Success Rate: {success_rate:6.2f}%")
