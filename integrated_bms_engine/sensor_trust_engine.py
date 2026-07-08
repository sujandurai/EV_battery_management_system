import os
import json
import numpy as np
import pandas as pd
import joblib
import onnxruntime as ort
import xgboost as xgb
import pickle

from .config import (
    SCALER_PATH, AUTOENCODER_PATH_ONNX, ISOLATION_FOREST_PATH,
    TRUST_THRESHOLDS_PATH, DETECTOR_CONFIG_PATH, ENGINEERED_FEATURES,
    REDUCED_FEATURES, SENSOR_GROUPS, WEIGHTS
)
from .feature_engineering import compute_features

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
            
        # 3. Load Supervised XGBoost Classifiers for Accuracy Supervision
        base_dir = os.path.dirname(os.path.abspath(__file__))
        xgb_status_path = os.path.join(base_dir, "models", "trust_status_classifier.json")
        xgb_sensor_path = os.path.join(base_dir, "models", "trust_sensor_classifier.json")
        status_encoder_path = os.path.join(base_dir, "models", "sensor_status_encoder.pkl")
        sensor_encoder_path = os.path.join(base_dir, "models", "faulty_sensor_encoder.pkl")
        
        if not all(os.path.exists(p) for p in [xgb_status_path, xgb_sensor_path, status_encoder_path, sensor_encoder_path]):
            raise FileNotFoundError("Supervised XGBoost files not found in noneed directory")
            
        device_param = 'cpu'
        try:
            import torch
            if torch.cuda.is_available():
                device_param = 'cuda'
        except Exception:
            pass
            
        self.clf_status = xgb.XGBClassifier(device=device_param)
        self.clf_status.load_model(xgb_status_path)
        
        self.clf_sensor = xgb.XGBClassifier(device=device_param)
        self.clf_sensor.load_model(xgb_sensor_path)
        
        self.le_status = joblib.load(status_encoder_path)
        self.le_sensor = joblib.load(sensor_encoder_path)
        
        self.xgb_features = [
            'time', 'Active_Cells', 'Current', 'Status', 'SoC', 'Cell1', 'Cell2', 'Cell3', 'Cell4', 'Pack_V', 
            'Min_Cell_V', 'Max_Cell_V', 'Cell_Imbalance', 'T1', 'T2', 'Delta_T', 'Vib_RMS', 'Vib_Peak', 
            'Vib_Freq', 'CO_PPM', 'dV1_dt', 'dV2_dt', 'dV3_dt', 'dV4_dt', 'dT1_dt', 'dT2_dt', 'dCO_dt', 
            'dI_dt', 'dImbalance_dt', 'dSoC_dt', 'V_rolling_std', 'T_rolling_mean',
            'Cell1_dev', 'Cell2_dev', 'Cell3_dev', 'Cell4_dev', 'T1_dev', 'T2_dev', 'Vib_Peak_dev',
            'Cell1_std_10', 'Cell2_std_10', 'Cell3_std_10', 'Cell4_std_10', 'Current_std_10', 'T1_std_10', 'T2_std_10', 'CO_PPM_std_10', 'Vib_RMS_std_10'
        ]
            
        # 4. Initialize ONNX Runtime Session for the Autoencoder
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        
        providers = ['CPUExecutionProvider']
        try:
            import torch
            if torch.cuda.is_available():
                providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        except Exception:
            pass
        self.ort_session = ort.InferenceSession(AUTOENCODER_PATH_ONNX, sess_options=opts, providers=providers)
        
        # 5. Sliding Window History Buffers
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
            if is_forest_anomaly:
                overall_trust = min(overall_trust, 50)
        elif self.winning_strategy == 3:
            is_forest_anomaly = (self.clf.predict(feat_errors)[0] == -1)
            if is_forest_anomaly:
                overall_trust = min(overall_trust, 50)
                
        # Step 6: Supervised XGBoost Accuracy Supervision Double-Check
        # Prepare 48 features for the XGBoost model
        df_xgb = df_feat.copy()
        
        # 1. Base features mapping & rolling std renames
        rename_dict = {
            'Cell1_roll_std': 'Cell1_std_10',
            'Cell2_roll_std': 'Cell2_std_10',
            'Cell3_roll_std': 'Cell3_std_10',
            'Cell4_roll_std': 'Cell4_std_10',
            'Current_roll_std': 'Current_std_10',
            'T1_roll_std': 'T1_std_10',
            'T2_roll_std': 'T2_std_10',
            'CO_PPM_roll_std': 'CO_PPM_std_10',
            'Vib_RMS_roll_std': 'Vib_RMS_std_10'
        }
        df_xgb = df_xgb.rename(columns=rename_dict)
        
        # 2. Add dev features
        cell_cols = ['Cell1', 'Cell2', 'Cell3', 'Cell4']
        cell_median = df_xgb[cell_cols].median(axis=1)
        for col in cell_cols:
            df_xgb[f"{col}_dev"] = df_xgb[col] - cell_median
            
        df_xgb['T1_dev'] = df_xgb['T1'] - df_xgb['T1_roll_mean']
        df_xgb['T2_dev'] = df_xgb['T2'] - df_xgb['T1_roll_mean']
        df_xgb['Vib_Peak_dev'] = df_xgb['Vib_Peak'] - 3.0 * df_xgb['Vib_RMS']
        
        # 3. Add V_rolling_std and T_rolling_mean
        df_xgb['V_rolling_std'] = df_xgb['Pack_V'].rolling(window=10, min_periods=1).std().fillna(0.0)
        df_xgb['T_rolling_mean'] = df_xgb['T1_roll_mean']
        
        # 4. Copy raw columns from df_buf
        for col in ['time', 'Active_Cells', 'SoC']:
            if col in df_buf.columns:
                df_xgb[col] = df_buf[col]
            else:
                if col == 'time':
                    df_xgb['time'] = 0.0
                elif col == 'Active_Cells':
                    df_xgb['Active_Cells'] = 4.0
                elif col == 'SoC':
                    df_xgb['SoC'] = 100.0
                    
        # 5. Compute dImbalance_dt and dSoC_dt
        if 'time' in df_xgb.columns:
            dt_xgb = df_xgb['time'].diff().replace(0, np.nan)
            dt_xgb = dt_xgb.bfill().fillna(1.0)
        else:
            dt_xgb = pd.Series([1.0] * len(df_xgb))
            
        df_xgb['dImbalance_dt'] = df_xgb['Cell_Imbalance'].diff() / dt_xgb
        df_xgb['dImbalance_dt'] = df_xgb['dImbalance_dt'].bfill().fillna(0.0)
        
        df_xgb['dSoC_dt'] = df_xgb['SoC'].diff() / dt_xgb
        df_xgb['dSoC_dt'] = df_xgb['dSoC_dt'].bfill().fillna(0.0)
        
        # 6. Map Status to numeric index
        STATUS_TO_INDEX = {'CHARGING': 0, 'DISCHARGING': 1, 'IDLE': 2}
        status_val = self.history_buffer[-1].get('Status', 'IDLE')
        df_xgb['Status'] = STATUS_TO_INDEX.get(str(status_val).upper(), 2)
        
        # Extract the latest sequence step features
        xgb_input = df_xgb.tail(1)[self.xgb_features]
        
        # Predict status and faulty sensor
        pred_status_idx = int(self.clf_status.predict(xgb_input)[0])
        pred_status_str = self.le_status.inverse_transform([pred_status_idx])[0]
        prob_status = self.clf_status.predict_proba(xgb_input)[0]
        
        pred_sensor_idx = int(self.clf_sensor.predict(xgb_input)[0])
        pred_sensor_str = self.le_sensor.inverse_transform([pred_sensor_idx])[0]
        
        # If the XGBoost supervised model detects a sensor fault (status is not HEALTHY) with high confidence:
        if pred_status_str != "HEALTHY" and prob_status[pred_status_idx] > 0.70:
            # Override overall_trust to a low value (below 40, to trigger SENSOR_FAULT)
            overall_trust = min(overall_trust, 30)
            
            # Map faulty sensor string (e.g. "Cell3", "Temperature") into anomalous_sensors
            if pred_sensor_str != "NONE" and pred_sensor_str not in anomalous_sensors:
                anomalous_sensors.append(pred_sensor_str)
                # Set the trust score for this specific sensor in smoothed_sensor_trusts to 10
                if pred_sensor_str in smoothed_sensor_trusts:
                    smoothed_sensor_trusts[pred_sensor_str] = 10
        
        # Calculate Anomaly Detection Confidence
        base_confidence = 1.0
        if self.winning_strategy == 1:
            if self.loss_type == 'weighted':
                mse = float(np.sum(self.feature_weights * feat_errors_flat))
            else:
                mse = float(np.mean(feat_errors_flat))
            base_confidence = float(np.clip(1.0 - (mse / (3.0 * self.detector_config.get('threshold', self.detector_config.get('mse_threshold', 0.05)))), 0.1, 1.0))
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
