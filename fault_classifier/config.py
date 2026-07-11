import os

# Directories and Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_PATH = os.path.join(BASE_DIR, "generated_battery_dataset", "combined_battery_dataset.csv")
REGULAR_DIR = os.path.join(BASE_DIR, "generated_battery_dataset", "regular_alt_batteries")
UNSEEN_DIR = os.path.join(BASE_DIR, "generated_battery_dataset", "recommissioned_batteries")
OUTPUT_DIR = os.path.join(BASE_DIR, "fault_classifier")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Serialized Asset Exports
MODEL_PTH_PATH = os.path.join(OUTPUT_DIR, "battery_fault_classifier.pth")
MODEL_ONNX_PATH = os.path.join(OUTPUT_DIR, "battery_fault_classifier.onnx")
SCALER_PATH = os.path.join(OUTPUT_DIR, "feature_scaler.joblib")
ENCODER_PATH = os.path.join(OUTPUT_DIR, "label_encoder.joblib")
CONFIG_JSON_PATH = os.path.join(OUTPUT_DIR, "best_model_config.json")
METRICS_JSON_PATH = os.path.join(OUTPUT_DIR, "fault_classifier_metrics.json")
REPORT_CSV_PATH = os.path.join(OUTPUT_DIR, "classification_report.csv")
CONFUSION_IMAGE_PATH = os.path.join(OUTPUT_DIR, "confusion_matrix.png")
IMPORTANCE_CSV_PATH = os.path.join(OUTPUT_DIR, "feature_importance.csv")
EVAL_REPORT_PATH = os.path.join(OUTPUT_DIR, "evaluation_report.md")

# Sensor Trust Configuration
TRUST_THRESHOLD = 80

# Primary Target Classes
PRIMARY_CLASSES = [
    "NORMAL",
    "CELL_IMBALANCE",
    "CELL_OVERVOLTAGE",
    "CELL_UNDERVOLTAGE",
    "OVERCURRENT_CHARGE",
    "OVERCURRENT_DISCHARGE",
    "OVERTEMPERATURE",
    "HIGH_VIBRATION",
    "GAS_LEAK",
    "THERMAL_RUNAWAY",
    "WEAK_CELL"
]

# Physical and Engineered Features to use
RAW_NUMERICAL_FEATURES = [
    "Current", "SoC",
    "Cell1", "Cell2", "Cell3", "Cell4",
    "Pack_V", "Min_Cell_V", "Max_Cell_V", "Cell_Imbalance",
    "T1", "T2", "Delta_T",
    "Vib_RMS", "Vib_Peak", "Vib_Freq",
    "CO_PPM"
]

DERIVATIVE_FEATURES = [
    "dV1_dt", "dV2_dt", "dV3_dt", "dV4_dt",
    "dT1_dt", "dT2_dt",
    "dCO_dt",
    "dI_dt",
    "dImbalance_dt",
    "dSoC_dt"
]

ROLLING_FEATURES = [
    "V_rolling_std", "T_rolling_mean"
]

# Status one-hot values
STATUS_VALUES = ["CHARGING", "DISCHARGING", "IDLE"]

# All input feature columns in exact order
INPUT_FEATURES = RAW_NUMERICAL_FEATURES + [f"Status_{val}" for val in STATUS_VALUES] + DERIVATIVE_FEATURES + ROLLING_FEATURES

# Hyperparameter Search Spaces for Optimization
PARAM_SPACE = {
    'hidden_size': [128, 256],
    'num_layers': [1],
    'dropout': [0.2, 0.3],
    'sequence_length': [20, 30, 40, 50],
    'learning_rate': [0.001, 0.0005],
    'architecture': ['LSTM', 'BiLSTM', 'LSTMAttention', 'BiLSTMAttention'],
    'loss_function': ['CE', 'Smoothed_CE', 'Focal'],
    'optimizer': ['AdamW', 'RMSprop'],
    'scheduler': ['ReduceLROnPlateau', 'CosineAnnealingLR']
}
