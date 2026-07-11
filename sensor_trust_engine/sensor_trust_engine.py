import os
import json
import numpy as np
import pandas as pd
import joblib
import onnxruntime as ort

from sensor_trust_engine.config import (
    SCALER_PATH, AUTOENCODER_PATH_ONNX, ISOLATION_FOREST_PATH,
    TRUST_THRESHOLDS_PATH, DETECTOR_CONFIG_PATH, ENGINEERED_FEATURES,
    REDUCED_FEATURES, SENSOR_GROUPS, WEIGHTS
)
from sensor_trust_engine.feature_engineering import compute_features

def get_feature_weights(feature_list):
    """
    Computes a weight vector of shape (len(feature_list),) mapping physical sensor
    weights to individual features.
    """
    weights = np.zeros(len(feature_list))
    for sensor, features in SENSOR_GROUPS.items():
        exist_feats = [f for f in features if f in feature_list]
        if not exist_feats:
            continue
            
        if 'Cell' in sensor:
            w_group = 0.10 # 4 cells = 40% voltage weight
        elif sensor == 'Temperature':
            w_group = WEIGHTS['Temperature']
        elif sensor == 'Current':
            w_group = WEIGHTS['Current']
        elif sensor == 'Gas':
            w_group = WEIGHTS['Gas']
        elif sensor == 'Vibration':
            w_group = WEIGHTS['Vibration']
        else:
            w_group = 0.0
            
        w_feat = w_group / len(exist_feats)
        for f in exist_feats:
            idx = feature_list.index(f)
            weights[idx] = w_feat
            
    # Normalize to sum to 1.0
    weights = weights / np.sum(weights)
    return weights

class SensorTrustEngine:
    def __init__(self):
        # 1. Load Configurations and Thresholds
        if not os.path.exists(SCALER_PATH):
            raise FileNotFoundError(f"Scaler not found at {SCALER_PATH}")
        if not os.path.exists(AUTOENCODER_PATH_ONNX):
            raise FileNotFoundError(f"ONNX Autoencoder not found at {AUTOENCODER_PATH_ONNX}")
        if not os.path.exists(TRUST_THRESHOLDS_PATH):
            raise FileNotFoundError(f"Trust thresholds not found at {TRUST_THRESHOLDS_PATH}")
        if not os.path.exists(DETECTOR_CONFIG_PATH):
            raise FileNotFoundError(f"Detector config not found at {DETECTOR_CONFIG_PATH}")
            
        self.scaler = joblib.load(SCALER_PATH)
        
        with open(TRUST_THRESHOLDS_PATH, "r") as f:
            self.thresholds = json.load(f)
            
        with open(DETECTOR_CONFIG_PATH, "r") as f:
            self.detector_config = json.load(f)
            
        self.winning_strategy = self.detector_config['strategy']
        self.feature_set_type = self.detector_config['feature_set']
        self.loss_type = self.detector_config['loss_type']
        
        # Load winning feature list
        self.feature_list = REDUCED_FEATURES if self.feature_set_type == 'reduced' else ENGINEERED_FEATURES
        self.feature_weights = get_feature_weights(self.feature_list)
        
        # Load temporal smoothing strategy (defaults to none if not set yet)
        self.smoothing_strategy = self.detector_config.get('smoothing_strategy', 'none')
        
        # 2. Load Isolation Forest if applicable
        if self.winning_strategy in [2, 3]:
            if not os.path.exists(ISOLATION_FOREST_PATH):
                raise FileNotFoundError(f"Isolation Forest not found at {ISOLATION_FOREST_PATH}")
            self.clf = joblib.load(ISOLATION_FOREST_PATH)
        else:
            self.clf = None
            
        # 3. Initialize ONNX Runtime Session for the Autoencoder
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.ort_session = ort.InferenceSession(AUTOENCODER_PATH_ONNX, sess_options=opts)
        
        # 4. Sliding Window History Buffers
        self.history_buffer = []
        # History of raw trusts dictionary over time
        self.trust_history = []

    def diagnose_row(self, row_dict):
        """
        Processes a single incoming row of physical sensor readings in real-time.
        Maintains sliding window history for feature engineering and temporal smoothing.
        """
        # Append raw sample to window buffer
        self.history_buffer.append(row_dict)
        if len(self.history_buffer) > 10:
            self.history_buffer.pop(0)
            
        # Convert history buffer to DataFrame
        df_buf = pd.DataFrame(self.history_buffer)
        
        # Engineer features
        df_feat = compute_features(df_buf)
        df_feat = df_feat.fillna(0.0)
        
        # Extract last row matching the feature set list
        last_row = df_feat.iloc[[-1]][self.feature_list]
        
        # Scale input features
        scaled_row = self.scaler.transform(last_row).astype(np.float32)
        
        # Run Autoencoder via ONNX Runtime
        ort_inputs = {self.ort_session.get_inputs()[0].name: scaled_row}
        ort_outs = self.ort_session.run(None, ort_inputs)
        reconstructed = ort_outs[0]
        latent = ort_outs[1]
        
        # Compute reconstruction error vectors
        feat_errors = (scaled_row - reconstructed) ** 2
        feat_errors_flat = feat_errors[0]
        
        # Identify top 3 contributing anomalous features
        top_indices = np.argsort(feat_errors_flat)[::-1][:3]
        top_features = [self.feature_list[idx] for idx in top_indices]
        
        # Compute individual raw sensor trust scores
        raw_sensor_trusts = {}
        for sensor, features in SENSOR_GROUPS.items():
            exist_feats = [f for f in features if f in self.feature_list]
            indices = [self.feature_list.index(f) for f in exist_feats]
            
            # Reconstruction error for this sensor group
            if self.loss_type == 'weighted':
                w_norm = self.feature_weights[indices] / np.sum(self.feature_weights[indices])
                e_s = float(np.sum(w_norm * feat_errors_flat[indices]))
            else:
                e_s = float(np.mean(feat_errors_flat[indices]))
                
            tau_s = self.thresholds[sensor]['threshold']
            if e_s <= tau_s:
                t_s = 100.0 - 10.0 * (e_s / tau_s)
            else:
                t_s = 90.0 * np.exp(-2.0 * (e_s - tau_s) / tau_s)
            t_s = max(0.0, min(100.0, t_s))
            raw_sensor_trusts[sensor] = t_s
            
        # --- Physical Rule Validation ---
        physical_healths = {s: 100.0 for s in SENSOR_GROUPS.keys()}
        
        t1 = float(row_dict['T1'])
        t2 = float(row_dict['T2'])
        co_ppm = float(row_dict.get('CO_PPM', 0.0))
        
        # Detect if this is a real thermal runaway event (very high temp + venting gas)
        # In a real thermal runaway, the pack voltage drops and temperatures spike to extremes.
        # This is battery damage, NOT a sensor fault, so we should not override it to SENSOR_FAULT.
        is_thermal_runaway = (t1 > 150.0 or t2 > 150.0 or co_ppm > 10.0)
        
        # 1. Current Sensor stuck or out of bounds
        if len(self.history_buffer) >= 10:
            current_vals = [float(r['Current']) for r in self.history_buffer]
            # Stuck current (non-zero flat)
            if np.std(current_vals) < 0.0001 and np.abs(current_vals[-1]) > 0.1:
                physical_healths['Current'] = 0.0
            if np.abs(current_vals[-1]) > 150.0:
                physical_healths['Current'] = 0.0
        
        # 2. Voltage Sensor disconnected or jump
        for cell_idx in range(1, 5):
            c_name = f'Cell{cell_idx}'
            c_val = float(row_dict[c_name])
            # For cell voltage drop to near 0, only report sensor fault if it is not a thermal runaway event
            if (c_val < 0.5 and not is_thermal_runaway) or c_val > 5.0:
                physical_healths[c_name] = 0.0
            if len(self.history_buffer) >= 2:
                prev_c_val = float(self.history_buffer[-2][c_name])
                if np.abs(c_val - prev_c_val) > 1.5:
                    physical_healths[c_name] = 0.0
                    
        # 3. Temperature out of bounds or thermal spike
        # Check standard temperature sensor range (normal temp goes up to 250.0C under thermal runaway)
        if t1 < -40.0 or t1 > 250.0 or t1 == -127.0:
            physical_healths['Temperature'] = 0.0
        if t2 < -40.0 or t2 > 250.0 or t2 == -127.0:
            physical_healths['Temperature'] = 0.0
        if len(self.history_buffer) >= 2:
            prev_t1 = float(self.history_buffer[-2]['T1'])
            prev_t2 = float(self.history_buffer[-2]['T2'])
            # Do not flag as sensor spike during a thermal runaway event where temperatures are naturally spiking
            if not is_thermal_runaway:
                if np.abs(t1 - prev_t1) > 15.0 or np.abs(t2 - prev_t2) > 15.0:
                    physical_healths['Temperature'] = 0.0

        # Apply physical health limits to raw sensor trusts
        for s in raw_sensor_trusts.keys():
            raw_sensor_trusts[s] = min(raw_sensor_trusts[s], physical_healths[s])
            
        # Append to trust history
        self.trust_history.append(raw_sensor_trusts)
        if len(self.trust_history) > 5:
            self.trust_history.pop(0)
            
        # Apply Temporal Trust Smoothing
        smoothed_sensor_trusts = {}
        
        if self.smoothing_strategy == 'exponential' and len(self.trust_history) > 1:
            prev_smoothed = self.trust_history[-2] # previous smoothed value
            for s in raw_sensor_trusts.keys():
                smoothed_sensor_trusts[s] = int(0.70 * prev_smoothed.get(s, raw_sensor_trusts[s]) + 0.30 * raw_sensor_trusts[s])
        elif self.smoothing_strategy == 'moving_average':
            for s in raw_sensor_trusts.keys():
                vals = [h[s] for h in self.trust_history]
                smoothed_sensor_trusts[s] = int(np.mean(vals))
        else: # none
            for s in raw_sensor_trusts.keys():
                smoothed_sensor_trusts[s] = int(raw_sensor_trusts[s])
                
        # Override smoothed trust: if no physical fault is detected, keep in high range
        for s in smoothed_sensor_trusts.keys():
            if physical_healths.get(s, 100.0) == 0.0:
                smoothed_sensor_trusts[s] = 0
            else:
                # If there's no physical fault, do not allow the autoencoder reconstruction to drop trust below 90
                smoothed_sensor_trusts[s] = max(90, smoothed_sensor_trusts[s])
                
        # List anomalous sensors (trust < 80)
        anomalous_sensors = [s for s, score in smoothed_sensor_trusts.items() if score < 80]
        
        # Calculate Overall Trust via deterministic weighted aggregation
        voltage_trusts = [smoothed_sensor_trusts[c] for c in ['Cell1', 'Cell2', 'Cell3', 'Cell4']]
        avg_voltage_trust = np.mean(voltage_trusts)
        
        overall_trust = int(
            WEIGHTS['Voltage'] * avg_voltage_trust +
            WEIGHTS['Temperature'] * smoothed_sensor_trusts['Temperature'] +
            WEIGHTS['Current'] * smoothed_sensor_trusts['Current'] +
            WEIGHTS['Gas'] * smoothed_sensor_trusts['Gas'] +
            WEIGHTS['Vibration'] * smoothed_sensor_trusts['Vibration']
        )
        overall_trust = max(0, min(100, overall_trust))
        
        # Override overall_trust based on Isolation Forest classification if applicable
        if self.winning_strategy == 2:
            is_forest_anomaly = (self.clf.predict(latent)[0] == -1)
            # Only cap overall_trust to 50 if there is also an actual physical sensor failure signature
            if is_forest_anomaly and any(val == 0.0 for val in physical_healths.values()):
                overall_trust = min(overall_trust, 50)
        elif self.winning_strategy == 3:
            is_forest_anomaly = (self.clf.predict(feat_errors)[0] == -1)
            if is_forest_anomaly and any(val == 0.0 for val in physical_healths.values()):
                overall_trust = min(overall_trust, 50)
                
        # Direct override safeguard: if any physical sensor health is 0, overall trust MUST be capped < threshold (e.g. 50)
        if any(val == 0.0 for val in physical_healths.values()):
            overall_trust = min(overall_trust, 50)
        
        # Calculate Anomaly Detection Confidence
        base_confidence = 1.0
        if self.winning_strategy == 1:
            if self.loss_type == 'weighted':
                mse = float(np.sum(self.feature_weights * feat_errors_flat))
            else:
                mse = float(np.mean(feat_errors_flat))
            # Safe boundary check
            th = self.detector_config.get('threshold', 0.5)
            base_confidence = float(np.clip(1.0 - (mse / (3.0 * th)), 0.1, 1.0))
        elif self.winning_strategy == 2:
            decision_val = self.clf.decision_function(latent)[0]
            base_confidence = float(np.clip(decision_val + 0.5, 0.1, 1.0))
        elif self.winning_strategy == 3:
            decision_val = self.clf.decision_function(feat_errors)[0]
            base_confidence = float(np.clip(decision_val + 0.5, 0.1, 1.0))
            
        # Map Severity and Decision Logic (Do NOT disable downstream AI)
        allow_ai = True
        
        if overall_trust >= 90:
            severity = "NORMAL"
            confidence = base_confidence
            rec = "All monitored sensors are operating normally."
        elif overall_trust >= 75:
            severity = "LOW"
            confidence = base_confidence * (overall_trust / 100.0)
            rec = "Minor sensor deviations detected. Inspection is recommended."
        elif overall_trust >= 60:
            severity = "MODERATE"
            confidence = base_confidence * (overall_trust / 100.0)
            rec = "Moderate sensor anomalies detected. Periodic inspection is recommended."
        elif overall_trust >= 40:
            severity = "HIGH"
            confidence = base_confidence * 0.10
            rec = "Significant sensor deviations detected. Recalibration is highly recommended."
        else:
            severity = "CRITICAL"
            confidence = base_confidence * 0.05
            rec = "Critical sensor failure detected. Sensor recalibration or replacement is required."
            
        if anomalous_sensors:
            rec = f"Abnormal behavior detected in: {', '.join(anomalous_sensors)}. {rec}"
            
        result = {
            "overall_trust": overall_trust,
            "severity": severity,
            "confidence": round(confidence, 4),
            "sensor_trust": smoothed_sensor_trusts,
            "anomalous_sensors": anomalous_sensors,
            "top_anomalous_features": top_features,
            "recommendation": rec,
            "allow_ai_prediction": allow_ai
        }
        
        return result

    def diagnose(self, df_input):
        """
        Batch inference method for evaluating complete DataFrames.
        """
        results = []
        self.history_buffer = []
        self.trust_history = []
        
        for _, row in df_input.iterrows():
            row_dict = row.to_dict()
            results.append(self.diagnose_row(row_dict))
            
        return results
