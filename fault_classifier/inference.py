import os
import sys
import json
import numpy as np
import pandas as pd
import joblib
import onnxruntime as ort

# Add parent directory and prioritize it to avoid config namespace conflicts
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir in sys.path:
    sys.path.remove(parent_dir)
sys.path.insert(0, parent_dir)

from fault_classifier.config import (
    MODEL_ONNX_PATH, SCALER_PATH, ENCODER_PATH, CONFIG_JSON_PATH,
    INPUT_FEATURES, PRIMARY_CLASSES, TRUST_THRESHOLD
)
from fault_classifier.feature_engineering import compute_physical_features
from sensor_trust_engine.sensor_trust_engine import SensorTrustEngine
from sensor_trust_engine.config import SENSOR_GROUPS

def softmax(x):
    """Compute softmax values for each sets of scores in x."""
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum(axis=-1, keepdims=True)

class FaultClassificationEngine:
    """
    Streaming Inference Engine for real-time battery fault classification.
    Maintains a sliding history buffer of raw telemetry, runs the Sensor Trust Engine
    to check sensor reliability, runs the 11-class LSTM battery fault classifier,
    and returns scaled confidence scores when trust is degraded.
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
        
        # 3. Load ONNX Runtime Session for 11-class Battery Fault Classifier
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.ort_session = ort.InferenceSession(MODEL_ONNX_PATH, sess_options=opts)
        
        # 4. Buffer State
        self.history_buffer = []
        self.predictions_history = []  # List of raw predicted class indices (max size 3)
        self.prob_history = []         # List of raw probability arrays (max size 3)
        
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
            # Warm-up phase: return default healthy normal state
            return {
                "prediction": "NORMAL",
                "probability": 1.0,
                "raw_prediction": "NORMAL",
                "source": "LSTM",
                "trust_diagnostics": {
                    "overall_trust": 100.0,
                    "sensor_trust": {s: 100.0 for s in SENSOR_GROUPS.keys()},
                    "anomalous_sensors": [],
                    "top_anomalous_features": [],
                    "allow_ai_prediction": True,
                    "severity": "NORMAL",
                    "confidence": 1.0,
                    "recommendation": "System healthy. Standard operations."
                }
            }
            
        # Step 1: Run Sensor Trust Engine on the latest row
        latest_row = self.history_buffer[-1]
        # Make a copy of the buffer to run the trust engine batches correctly
        trust_df = pd.DataFrame(self.history_buffer)
        trust_results = self.trust_engine.diagnose(trust_df)
        trust_result = trust_results[-1] # Latest row trust metrics
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
            
        # Step 3: Combine predictions based on trust levels
        if overall_trust < self.trust_threshold:
            # Low trust: report SENSOR_FAULT, but still execute and return LSTM prediction scaled down
            scaled_down_prob = float(final_prob * (overall_trust / 100.0))
            return {
                "prediction": "SENSOR_FAULT",
                "probability": round(1.0 - (overall_trust / 100.0), 4),
                "raw_prediction": "SENSOR_FAULT",
                "source": "Sensor Trust Engine",
                "battery_fault_prediction": {
                    "prediction": final_pred_class,
                    "probability": round(scaled_down_prob, 4),
                    "source": "LSTM (Scale-Down Confidence)"
                },
                "trust_diagnostics": trust_result
            }
        else:
            # High trust: execute and return LSTM prediction normally
            return {
                "prediction": final_pred_class,
                "probability": round(final_prob, 4),
                "raw_prediction": raw_pred_class,
                "source": "LSTM",
                "trust_diagnostics": trust_result
            }
