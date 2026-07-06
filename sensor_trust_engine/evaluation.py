import os
import time
import json
import numpy as np
import pandas as pd
import random
import psutil
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support, roc_auc_score,
    confusion_matrix, average_precision_score, balanced_accuracy_score,
    matthews_corrcoef
)

from sensor_trust_engine.config import (
    PREDICTIONS_CSV_PATH, METRICS_JSON_PATH, DETECTOR_CONFIG_PATH, RAW_SENSORS,
    SCALER_PATH, AUTOENCODER_PATH_ONNX, ISOLATION_FOREST_PATH
)
from utils import get_memory_usage
from sensor_trust_engine.sensor_trust_engine import SensorTrustEngine

def inject_block(block_df, status, sensor):
    """
    Applies fault injection to a contiguous block DataFrame.
    """
    block_df = block_df.copy().reset_index(drop=True)
    block_size = len(block_df)
    
    if status == 'HEALTHY':
        return block_df
        
    if status == 'CALIBRATION_FAULT':
        val_offset = 0.35 if 'Cell' in sensor else (8.0 if sensor == 'Current' else 12.0)
        block_df[sensor] += val_offset
    elif status == 'SIGNAL_FAULT':
        noise = np.random.normal(0, 0.05 if 'Cell' in sensor else 2.5, size=block_size)
        block_df[sensor] += noise
    elif status == 'STUCK_SENSOR':
        frozen_val = block_df.loc[0, sensor]
        block_df[sensor] = frozen_val
    elif status == 'COMMUNICATION_FAULT':
        block_df[sensor] = np.nan
        
    return block_df

def run_streaming_segments(segments_data, engine):
    """
    Simulates streaming row-by-row diagnostics over multiple contiguous segments,
    clearing the history buffer at the start of each segment to replicate real BMS startup.
    """
    predictions = []
    latencies = []
    
    # Warm up ONNX session
    engine.history_buffer = []
    engine.trust_history = []
    if segments_data:
        engine.diagnose_row(segments_data[0]['df'].iloc[0].to_dict())
        
    for seg in segments_data:
        # Clear buffer at segment start (new chronological sequence)
        engine.history_buffer = []
        engine.trust_history = []
        
        seg_df = seg['df']
        for _, row in seg_df.iterrows():
            row_dict = row.to_dict()
            t0 = time.perf_counter()
            pred = engine.diagnose_row(row_dict)
            latencies.append((time.perf_counter() - t0) * 1000) # in ms
            predictions.append(pred)
            
    return predictions, latencies

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    
    mem_before = get_memory_usage()
    
    battery_path = r"d:\battery 11\generated_battery_dataset\regular_alt_batteries\battery00.csv"
    if len(sys.argv) > 1:
        battery_path = sys.argv[1]
        
    if not os.path.exists(battery_path):
        print(f"Error: battery file not found at {battery_path}")
        return
        
    print(f"Loading raw battery file {os.path.basename(battery_path)}...")
    raw_df = pd.read_csv(battery_path)
    if 'start_time' in raw_df.columns:
        raw_df = raw_df.drop(columns=['start_time'])
        
    # Extract contiguous normal segments
    raw_df['is_normal'] = raw_df['Fault_Name'] == 'NORMAL'
    raw_df['group'] = (raw_df['is_normal'] != raw_df['is_normal'].shift()).cumsum()
    
    normal_groups = raw_df[raw_df['is_normal']].groupby('group')
    valid_segments = []
    for g_id, g_df in normal_groups:
        if len(g_df) >= 60: # Keep segments with at least 6 blocks
            valid_segments.append(g_df.copy().reset_index(drop=True))
            
    print(f"Found {len(valid_segments)} contiguous normal segments of length >= 60.")
    
    # We take the first 15 segments to create a validation set of ~3,000 to 4,000 samples
    selected_segments = valid_segments[:15]
    
    # Assign statuses to blocks within each segment
    random.seed(42)
    statuses = ['HEALTHY', 'CALIBRATION_FAULT', 'SIGNAL_FAULT', 'STUCK_SENSOR', 'COMMUNICATION_FAULT']
    
    segments_data = []
    global_row_counter = 0
    eval_indices = []
    y_true_list = []
    
    for seg_idx, seg_df in enumerate(selected_segments):
        # Slices seg_df into blocks of size 10
        block_size = 10
        num_blocks = len(seg_df) // block_size
        
        block_dfs = []
        for b in range(num_blocks):
            start_row = b * block_size
            end_row = start_row + block_size
            block_df = seg_df.iloc[start_row:end_row].copy()
            
            # Fault type assignment
            status = statuses[(seg_idx + b) % len(statuses)]
            sensor = random.choice(RAW_SENSORS) if status != 'HEALTHY' else 'NONE'
            
            injected = inject_block(block_df, status, sensor)
            injected['Sensor_Status_GT'] = status
            block_dfs.append(injected)
            
            # The 10th row of each block is evaluated, EXCEPT the first block of the segment
            # (which is skipped because the history buffer has size < 10)
            if b >= 1:
                eval_indices.append(global_row_counter + start_row + block_size - 1)
                y_true_list.append(0 if status == 'HEALTHY' else 1)
                
        seg_combined = pd.concat(block_dfs, ignore_index=True)
        segments_data.append({
            'df': seg_combined,
            'num_blocks': num_blocks
        })
        global_row_counter += len(seg_combined)
        
    val_combined_df = pd.concat([s['df'] for s in segments_data], ignore_index=True)
    y_true = np.array(y_true_list)
    
    # Instantiate engine
    engine = SensorTrustEngine()
    
    print("\n=== 1. Tuning Temporal Trust Smoothing Strategies ===")
    smoothing_options = ['none', 'exponential', 'moving_average']
    best_smooth_f1 = -1.0
    best_smooth_fpr = 1.0
    best_smoothing = 'none'
    
    for s_opt in smoothing_options:
        engine.smoothing_strategy = s_opt
        preds, _ = run_streaming_segments(segments_data, engine)
        
        y_pred = np.array([int(preds[i]['overall_trust'] < 90) for i in eval_indices])
        prec, rec, f1, _ = precision_recall_fscore_support(y_true, y_pred, average='binary', zero_division=0)
        tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel()
        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
        
        print(f"Smoothing: {s_opt:<15} | F1: {f1:.4f} | FPR: {fpr:.4f} | Recall: {rec:.4f}")
        
        if f1 > best_smooth_f1 or (abs(f1 - best_smooth_f1) < 1e-4 and fpr < best_smooth_fpr):
            best_smooth_f1 = f1
            best_smooth_fpr = fpr
            best_smoothing = s_opt
            
    print(f"Winning Smoothing Strategy: {best_smoothing}")
    
    # Save the selected smoothing strategy to detector_config.json
    with open(DETECTOR_CONFIG_PATH, "r") as f:
        config_data = json.load(f)
    config_data['smoothing_strategy'] = best_smoothing
    with open(DETECTOR_CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)
        
    # Re-instantiate and run with the optimized smoothing strategy
    engine = SensorTrustEngine()
    
    t_init = time.perf_counter()
    mem_after = get_memory_usage()
    model_mem_mb = mem_after - mem_before
    
    print("\nRunning final optimized streaming evaluation...")
    predictions, latencies = run_streaming_segments(segments_data, engine)
    
    avg_latency = float(np.mean(latencies))
    p95_latency = float(np.percentile(latencies, 95))
    cpu_util = float(psutil.cpu_percent(interval=0.1))
    
    # Calculate Extended Metrics
    y_pred = np.array([int(predictions[i]['overall_trust'] < 90) for i in eval_indices])
    y_score = np.array([1.0 - (predictions[i]['overall_trust'] / 100.0) for i in eval_indices])
    
    acc = float(accuracy_score(y_true, y_pred))
    p, r, f1, _ = precision_recall_fscore_support(y_true, y_pred, average='binary')
    roc_auc = float(roc_auc_score(y_true, y_score))
    pr_auc = float(average_precision_score(y_true, y_score))
    bal_acc = float(balanced_accuracy_score(y_true, y_pred))
    mcc = float(matthews_corrcoef(y_true, y_pred))
    
    cm = confusion_matrix(y_true, y_pred)
    tn, fp, fn, tp = cm.ravel()
    fpr = float(fp / (fp + tn))
    fnr = float(fn / (fn + tp))
    
    # Compute average detection delay in samples
    # We estimate detection delay across the faulty blocks
    delays = []
    global_b_counter = 0
    for seg in segments_data:
        for b in range(seg['num_blocks']):
            # Skip first block of the segment (skipped in evaluation)
            if b == 0:
                continue
            # We assign statuses based on: statuses[(seg_idx + b) % len(statuses)]
            # HEALTHY corresponds to index 0 of statuses
            # So if (seg_idx + b) % 5 == 0, it's a healthy block
            if (seg_idx + b) % 5 == 0:
                continue
                
            # Faulty block: find delay (offset from block start index)
            start_row_in_combined = global_b_counter * 10
            detected = False
            for t in range(10):
                pred_idx = start_row_in_combined + t
                if predictions[pred_idx]['overall_trust'] < 90:
                    delays.append(t)
                    detected = True
                    break
            if not detected:
                delays.append(10)
            global_b_counter += 1
            
    det_delay = float(np.mean(delays)) if delays else 0.0
    
    print("\n=== EXTENDED PERFORMANCE SUMMARY (OPTIMIZED PIPELINE) ===")
    print(f"Accuracy: {acc*100:.2f}% | Balanced Accuracy: {bal_acc*100:.2f}%")
    print(f"Precision: {p:.4f} | Recall: {r:.4f} | F1-Score: {f1:.4f}")
    print(f"ROC-AUC: {roc_auc:.4f} | PR-AUC: {pr_auc:.4f} | MCC: {mcc:.4f}")
    print(f"False Positive Rate (FPR): {fpr*100:.2f}% | False Negative Rate (FNR): {fnr*100:.2f}%")
    print(f"Average Detection Delay: {det_delay:.2f} samples")
    print(f"Avg Latency: {avg_latency:.4f} ms | 95th Percentile: {p95_latency:.4f} ms | CPU Util: {cpu_util:.1f}%")
    print("Confusion Matrix:")
    print(cm)
    
    # Save Predictions CSV
    pred_rows = []
    for idx, pred in enumerate(predictions):
        row_data = {
            'overall_trust': pred['overall_trust'],
            'severity': pred['severity'],
            'confidence': pred['confidence'],
            'allow_ai_prediction': pred['allow_ai_prediction'],
            'Sensor_Status_GT': val_combined_df.loc[idx, 'Sensor_Status_GT']
        }
        for s, score in pred['sensor_trust'].items():
            row_data[f'{s}_trust'] = score
        pred_rows.append(row_data)
        
    pred_df = pd.DataFrame(pred_rows)
    pred_df.to_csv(PREDICTIONS_CSV_PATH, index=False)
    
    # Save Metrics JSON
    metrics = {
        'accuracy': acc,
        'balanced_accuracy': bal_acc,
        'precision': float(p),
        'recall': float(r),
        'f1_score': float(f1),
        'roc_auc': roc_auc,
        'pr_auc': pr_auc,
        'mcc': mcc,
        'false_positive_rate': fpr,
        'false_negative_rate': fnr,
        'average_detection_delay_samples': det_delay,
        'average_latency_ms': avg_latency,
        'p95_latency_ms': p95_latency,
        'cpu_utilization_percent': cpu_util,
        'model_memory_usage_mb': model_mem_mb,
        'confusion_matrix': {
            'tn': int(tn),
            'fp': int(fp),
            'fn': int(fn),
            'tp': int(tp)
        }
    }
    
    with open(METRICS_JSON_PATH, "w") as f:
        json.dump(metrics, f, indent=4)
        
    # Generate evaluation_report.md
    report_content = f"""# Sensor Trust Engine Optimization & Evaluation Report

This report summarizes the results of the hyperparameter optimization, threshold tuning, feature ablation, scaling selections, and temporal trust smoothing evaluations for the unsupervised **Sensor Trust Engine** running on the Qualcomm QRB2210 Linux subsystem.

## 1. Selected Optimized Configuration
The combinatorial search selected the overall best pipeline based on F1-score and low False Positive Rate priority:

- **Anomaly Detection Strategy:** Strategy {config_data['strategy']}
- **Feature Scaler:** {config_data['scaler_type'].capitalize()}Scaler
- **Feature Set:** {config_data['feature_set'].capitalize()} Set ({49 if config_data['feature_set'] == 'full' else 44} features)
- **Autoencoder Bottleneck Size (Latent Dim):** {config_data['latent_dim']}
- **Reconstruction Loss Type:** {config_data['loss_type'].capitalize()} reconstruction loss
- **Temporal Trust Smoothing:** {best_smoothing.capitalize()} smoothing

## 2. Extended Performance Metrics (Contiguous Validation Split)
The optimized engine was evaluated on contiguous sequences with injected drift, noise, stuck, and communication faults:

* **Accuracy:** {acc*100:.2f}%
* **Balanced Accuracy:** {bal_acc*100:.2f}%
* **Precision:** {p:.4f}
* **Recall:** {r:.4f}
* **F1-Score:** {f1:.4f}
* **ROC-AUC:** {roc_auc:.4f}
* **PR-AUC:** {pr_auc:.4f}
* **Matthews Correlation Coefficient (MCC):** {mcc:.4f}
* **False Positive Rate (FPR):** {fpr*100:.2f}% (Target: <15%)
* **False Negative Rate (FNR):** {fnr*100:.2f}%
* **Average Detection Delay:** {det_delay:.2f} samples

## 3. Real-Time Resource Profile
* **Average Inference Latency:** {avg_latency:.4f} ms per sample
* **95th Percentile Latency:** {p95_latency:.4f} ms per sample
* **CPU Utilization:** {cpu_util:.1f}%
* **Model Memory Overhead:** {model_mem_mb:.2f} MB

## 4. Confusion Matrix
```
Predicted Healthy   Predicted Anomalous
TN: {tn:<10d}      FP: {fp:<10d}
FN: {fn:<10d}      TP: {tp:<10d}
```
"""
    report_path = os.path.join(os.path.dirname(METRICS_JSON_PATH), "evaluation_report.md")
    with open(report_path, "w") as f:
        f.write(report_content)
    print(f"Saved evaluation report to {report_path}")

if __name__ == "__main__":
    main()
