import os
import sys
import json
import numpy as np
import pandas as pd
import joblib
import onnxruntime as ort
import xgboost as xgb

# Add parent directory and prioritize it to avoid config namespace conflicts
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir in sys.path:
    sys.path.remove(parent_dir)
sys.path.insert(0, parent_dir)

from fault_classifier.config import (
    MODEL_ONNX_PATH, SCALER_PATH, ENCODER_PATH, CONFIG_JSON_PATH,
    INPUT_FEATURES, PRIMARY_CLASSES, TRUST_THRESHOLD,
    SOH_MODEL_PATH, RUL_MODEL_PATH, RISK_MODEL_PATH
)
from fault_classifier.feature_engineering import compute_physical_features
from .sensor_trust_engine import SensorTrustEngine

def softmax(x):
    """Compute softmax values for each sets of scores in x."""
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum(axis=-1, keepdims=True)

class FaultClassificationEngine:
    """
    Streaming Inference Engine for real-time battery fault classification,
    State of Health (SOH) estimation, and Remaining Useful Life (RUL) prediction.
    """
    def __init__(self):
        # 1. Load Configurations and Preprocessing Assets
        if not os.path.exists(CONFIG_JSON_PATH):
            raise FileNotFoundError(f"Config metadata not found at {CONFIG_JSON_PATH}")
        if not os.path.exists(SCALER_PATH):
            raise FileNotFoundError(f"Scaler not found at {SCALER_PATH}")
        if not os.path.exists(ENCODER_PATH):
            raise FileNotFoundError(f"Encoder not found at {ENCODER_PATH}")
        if not os.path.exists(MODEL_ONNX_PATH):
            raise FileNotFoundError(f"ONNX model not found at {MODEL_ONNX_PATH}")
        if not os.path.exists(SOH_MODEL_PATH):
            raise FileNotFoundError(f"SOH regressor model not found at {SOH_MODEL_PATH}")
        if not os.path.exists(RUL_MODEL_PATH):
            raise FileNotFoundError(f"RUL regressor model not found at {RUL_MODEL_PATH}")
        if not os.path.exists(RISK_MODEL_PATH):
            raise FileNotFoundError(f"Risk regressor model not found at {RISK_MODEL_PATH}")
            
        with open(CONFIG_JSON_PATH, "r") as f:
            self.config = json.load(f)
            
        self.scaler = joblib.load(SCALER_PATH)
        self.encoder = joblib.load(ENCODER_PATH)
        
        self.sequence_length = self.config['sequence_length']
        self.smoothing_strategy = self.config.get('smoothing_strategy', 'Strategy C')
        
        # Load configurable trust threshold
        self.trust_threshold = self.config.get('trust_threshold', TRUST_THRESHOLD)
        
        # 2. Instantiate Sensor Trust Engine
        self.trust_engine = SensorTrustEngine()
        
        # 3. Detect and configure GPU if available
        device_param = 'cpu'
        try:
            import torch
            if torch.cuda.is_available():
                device_param = 'cuda'
        except Exception:
            pass
            
        print(f"[XGBoost] Loading regressors using device: {device_param}")
        
        # Load XGBoost Regressor Models
        self.reg_soh = xgb.XGBRegressor(device=device_param)
        self.reg_soh.load_model(SOH_MODEL_PATH)
        
        self.reg_rul = xgb.XGBRegressor(device=device_param)
        self.reg_rul.load_model(RUL_MODEL_PATH)
        
        self.reg_risk = xgb.XGBRegressor(device=device_param)
        self.reg_risk.load_model(RISK_MODEL_PATH)
        
        # Exact 32-feature order required by regressors
        self.reg_features = [
            'time', 'Active_Cells', 'Current', 'Status', 'SoC', 'Cell1', 'Cell2', 'Cell3', 'Cell4', 'Pack_V', 
            'Min_Cell_V', 'Max_Cell_V', 'Cell_Imbalance', 'T1', 'T2', 'Delta_T', 'Vib_RMS', 'Vib_Peak', 
            'Vib_Freq', 'CO_PPM', 'dV1_dt', 'dV2_dt', 'dV3_dt', 'dV4_dt', 'dT1_dt', 'dT2_dt', 'dCO_dt', 
            'dI_dt', 'dImbalance_dt', 'dSoC_dt', 'V_rolling_std', 'T_rolling_mean'
        ]
        
        # 4. Load ONNX Runtime Session for 11-class Battery Fault Classifier
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
        print(f"[ONNX Runtime] Loading Fault Classifier using providers: {providers}")
        self.ort_session = ort.InferenceSession(MODEL_ONNX_PATH, sess_options=opts, providers=providers)
        
        # 4. Buffer State
        self.history_buffer = []
        self.predictions_history = []  # List of raw predicted class indices (max size 3)
        self.prob_history = []         # List of raw probability arrays (max size 3)
        
        # 5. Physics state tracking for SOH and RUL
        self.soh = None
        self.last_time = None
        
    def add_row(self, row_dict):
        """
        Appends a raw streaming telemetry row to the sliding history buffer.
        """
        self.history_buffer.append(row_dict)
        if len(self.history_buffer) > self.sequence_length:
            self.history_buffer.pop(0)
            
    def predict(self):
        """
        Performs inference on the current sliding window.
        Step 1: Check sensor reliability using Sensor Trust Engine.
        Step 2: Run LSTM Battery Fault Classifier.
        Step 3: Combine decisions according to trust levels.
        """
        if len(self.history_buffer) < self.sequence_length:
            # Warm-up phase: return default healthy normal state with standard SOH/RUL values
            return {
                "prediction": "NORMAL",
                "probability": 1.0,
                "raw_prediction": "NORMAL",
                "source": "LSTM",
                "soh_pct": 100.0,
                "soh_confidence": 1.0,
                "rul_cycles": 1000,
                "rul_confidence": 1.0,
                "risk_score": 0.0
            }
            
        # Step 1: Run Sensor Trust Engine on the latest row
        latest_row = self.history_buffer[-1]
        trust_result = self.trust_engine.diagnose_row(latest_row)
        overall_trust = trust_result['overall_trust']
        
        # Step 2: Run 11-Class LSTM Battery Fault Classifier
        df_buf = pd.DataFrame(self.history_buffer)
        df_feat = compute_physical_features(df_buf)
        df_feat = df_feat.fillna(0.0)
        
        # Extract features matching list
        last_rows = df_feat[INPUT_FEATURES].values.astype(np.float32)
        
        # Scale numerical features
        scaled = self.scaler.transform(last_rows).astype(np.float32)
        
        # Reshape to ONNX input: (batch_size=1, sequence_length, features)
        onnx_input = np.expand_dims(scaled, axis=0)
        
        # Run ONNX Runtime
        ort_inputs = {self.ort_session.get_inputs()[0].name: onnx_input}
        ort_outs = self.ort_session.run(None, ort_inputs)
        logits = ort_outs[0][0] # Shape: (11,)
        
        # Compute Softmax Probabilities
        probs = softmax(logits)
        raw_pred_idx = int(np.argmax(probs))
        raw_pred_class = self.encoder.inverse_transform([raw_pred_idx])[0]
        
        # Maintain history queues of size 3 for smoothing
        self.predictions_history.append(raw_pred_idx)
        if len(self.predictions_history) > 3:
            self.predictions_history.pop(0)
            
        self.prob_history.append(probs)
        if len(self.prob_history) > 3:
            self.prob_history.pop(0)
            
        # Apply Temporal Smoothing Strategy
        final_pred_class = raw_pred_class
        final_prob = float(probs[raw_pred_idx])
        
        if self.smoothing_strategy == 'Strategy B' and len(self.predictions_history) == 3:
            counts = np.bincount(self.predictions_history)
            final_pred_idx = int(np.argmax(counts))
            final_pred_class = self.encoder.inverse_transform([final_pred_idx])[0]
            voted_probs = [p[final_pred_idx] for p in self.prob_history]
            final_prob = float(np.mean(voted_probs))
            
        elif self.smoothing_strategy == 'Strategy C' and len(self.prob_history) == 3:
            avg_probs = np.mean(self.prob_history, axis=0)
            final_pred_idx = int(np.argmax(avg_probs))
            final_pred_class = self.encoder.inverse_transform([final_pred_idx])[0]
            final_prob = float(avg_probs[final_pred_idx])
            
        # Step 3: Direct Physical SOH & RUL Estimation (State Integration)
        current_time = latest_row.get('time', 0.0)
        t1 = latest_row.get('T1', 22.0)
        current = latest_row.get('Current', 0.0)
        soc = latest_row.get('SoC', 100.0)
        
        if self.last_time is None:
            self.last_time = current_time
            dt = 1.0
        else:
            dt = current_time - self.last_time
            self.last_time = current_time
            if dt <= 0.0:
                dt = 1.0
                
        if self.soh is None:
            self.soh = latest_row.get('SOH', 100.0)
            if self.soh is None or pd.isna(self.soh):
                self.soh = 100.0
                
        # SOH degradation (Cumulative Damage model)
        import math
        f_temp = math.exp((t1 - 25.0) / 15.0)
        f_current = (current / 5.0) ** 2
        f_soc = 1.0 + math.exp((soc - 80.0) / 10.0) + math.exp((20.0 - soc) / 10.0)
        
        dsoh = -(1e-6 * f_temp + 1e-6 * f_current + 2e-7 * f_soc) * dt
        self.soh = max(70.0, self.soh + dsoh)
        
        soh_val = self.soh
        
        # RUL (cycles) - Derived from SOH
        rul_val = (soh_val - 70.0) / 30.0 * 700.0
        rul_val = max(0.0, min(700.0, rul_val))
        
        # Output range validation
        soh_val = max(0.0, min(100.0, soh_val))
        
        # Step 4: Direct Physical Risk Score Engine
        c1 = latest_row.get('Cell1', 4.1)
        c2 = latest_row.get('Cell2', 4.1)
        c3 = latest_row.get('Cell3', 4.1)
        c4 = latest_row.get('Cell4', 4.1)
        
        max_cell_v = max(c1, c2, c3, c4)
        min_cell_v = min(c1, c2, c3, c4)
        cell_imb = max_cell_v - min_cell_v
        t2 = latest_row.get('T2', 22.0)
        vib_rms = latest_row.get('Vib_RMS', 0.0)
        vib_peak = latest_row.get('Vib_Peak', 0.0)
        co_ppm = latest_row.get('CO_PPM', 0.5)
        
        # Normalized deviations relative to boundary thresholds
        d_v_high = max(0.0, (max_cell_v - 4.20) / (4.65 - 4.20))
        d_v_low = max(0.0, (3.20 - min_cell_v) / (3.20 - 2.50))
        d_i_charge = max(0.0, (current - 6.0) / (10.0 - 6.0))
        d_i_discharge = max(0.0, (-8.0 - current) / (20.0 - 8.0))
        d_t1 = max(0.0, (t1 - 45.0) / (110.0 - 45.0))
        d_t2 = max(0.0, (t2 - 40.0) / (105.0 - 40.0))
        d_dt = max(0.0, ((t1 - t2) - 5.0) / (30.0 - 5.0))
        d_imb = max(0.0, (cell_imb - 0.03) / (0.50 - 0.03))
        d_vib = max(0.0, (vib_rms - 0.3) / (12.0 - 0.3))
        d_vib_peak = max(0.0, (vib_peak - 1.2) / (40.0 - 1.2))
        d_co = max(0.0, (co_ppm - 2.0) / (60.0 - 2.0))
        
        max_deviation = max(d_v_high, d_v_low, d_i_charge, d_i_discharge, d_t1, d_t2, d_dt, d_imb, d_vib, d_vib_peak, d_co)
        
        if max_deviation == 0.0:
            risk_val = 10.0
        else:
            risk_val = 20.0 + 80.0 * max_deviation
            
        risk_val = max(0.0, min(100.0, risk_val))
        
        # Scale confidence values dynamically when sensor trust is degraded
        soh_confidence = round(float(overall_trust / 100.0), 4)
        rul_confidence = round(float(overall_trust / 100.0), 4)
        
        # Determine output diagnostic predictions based on trust levels
        CRITICAL_BATTERY_FAULTS = {
            "OVERTEMPERATURE", "THERMAL_RUNAWAY", "GAS_LEAK", "HIGH_VIBRATION", 
            "CELL_UNDERVOLTAGE", "CELL_OVERVOLTAGE", "OVERCURRENT_CHARGE", 
            "OVERCURRENT_DISCHARGE", "CELL_IMBALANCE", "WEAK_CELL"
        }
        
        is_sensor_fault = (
            overall_trust < self.trust_threshold and 
            (final_pred_class not in CRITICAL_BATTERY_FAULTS or final_prob < 0.65)
        )
        
        # Combine everything into output payload
        if is_sensor_fault:
            # Low trust and not a confident critical battery fault: report SENSOR_FAULT
            scaled_down_prob = float(final_prob * (overall_trust / 100.0))
            return {
                "prediction": "SENSOR_FAULT",
                "probability": round(1.0 - (overall_trust / 100.0), 4),
                "raw_prediction": "SENSOR_FAULT",
                "source": "Sensor Trust Engine",
                "soh_pct": round(soh_val, 2),
                "soh_confidence": soh_confidence,
                "rul_cycles": int(max(0, round(rul_val))),
                "rul_confidence": rul_confidence,
                "risk_score": round(risk_val, 2),
                "battery_fault_prediction": {
                    "prediction": final_pred_class,
                    "probability": round(scaled_down_prob, 4),
                    "source": "LSTM (Scale-Down Confidence)"
                }
            }
        else:
            # High trust or confident critical battery fault: return battery classifier predictions
            return {
                "prediction": final_pred_class,
                "probability": round(final_prob, 4),
                "raw_prediction": raw_pred_class,
                "source": "LSTM",
                "soh_pct": round(soh_val, 2),
                "soh_confidence": soh_confidence,
                "rul_cycles": int(max(0, round(rul_val))),
                "rul_confidence": rul_confidence,
                "risk_score": round(risk_val, 2)
            }
