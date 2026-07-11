import os

# Central Configuration for the Sensor Trust Engine

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_PATH = os.path.join(BASE_DIR, "generated_battery_dataset", "combined_battery_dataset.csv")

SCALER_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "feature_scaler.joblib")
AUTOENCODER_PATH_PTH = os.path.join(BASE_DIR, "sensor_trust_engine", "sensor_autoencoder.pth")
AUTOENCODER_PATH_ONNX = os.path.join(BASE_DIR, "sensor_trust_engine", "sensor_autoencoder.onnx")
ISOLATION_FOREST_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "isolation_forest.joblib")
TRUST_THRESHOLDS_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "trust_thresholds.json")
DETECTOR_CONFIG_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "detector_config.json")
PREDICTIONS_CSV_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "sensor_trust_predictions.csv")
METRICS_JSON_PATH = os.path.join(BASE_DIR, "sensor_trust_engine", "sensor_trust_metrics.json")


# Physical sensors
RAW_SENSORS = [
    'Cell1', 'Cell2', 'Cell3', 'Cell4',
    'Current',
    'T1', 'T2',
    'CO_PPM',
    'Vib_RMS', 'Vib_Peak', 'Vib_Freq'
]

# Derived physics and time derivative features
DERIVED_PHYSICS = [
    'Pack_V', 'Min_Cell_V', 'Max_Cell_V', 'Cell_Imbalance', 'Delta_T'
]

DERIVATIVES = [
    'dV1_dt', 'dV2_dt', 'dV3_dt', 'dV4_dt',
    'dT1_dt', 'dT2_dt',
    'dI_dt',
    'dCO_dt',
    'dVib_RMS_dt', 'dVib_Peak_dt', 'dVib_Freq_dt'
]

REDUCED_DERIVATIVES = [
    'dV1_dt', 'dV2_dt', 'dV3_dt', 'dV4_dt',
    'dT1_dt', 'dT2_dt'
]

# Rolling window columns (10-sample windows)
ROLLING_MEANS = [f"{s}_roll_mean" for s in RAW_SENSORS]
ROLLING_STDS = [f"{s}_roll_std" for s in RAW_SENSORS]

# Complete engineered feature list for the Autoencoder and Anomaly Detector
ENGINEERED_FEATURES = RAW_SENSORS + DERIVED_PHYSICS + DERIVATIVES + ROLLING_MEANS + ROLLING_STDS
REDUCED_FEATURES = RAW_SENSORS + DERIVED_PHYSICS + REDUCED_DERIVATIVES + ROLLING_MEANS + ROLLING_STDS

# Target mappings to group physical sensors for trust scores
SENSOR_GROUPS = {
    'Cell1': ['Cell1', 'Cell1_roll_mean', 'Cell1_roll_std', 'dV1_dt'],
    'Cell2': ['Cell2', 'Cell2_roll_mean', 'Cell2_roll_std', 'dV2_dt'],
    'Cell3': ['Cell3', 'Cell3_roll_mean', 'Cell3_roll_std', 'dV3_dt'],
    'Cell4': ['Cell4', 'Cell4_roll_mean', 'Cell4_roll_std', 'dV4_dt'],
    'Current': ['Current', 'Current_roll_mean', 'Current_roll_std', 'dI_dt'],
    'Temperature': ['T1', 'T2', 'T1_roll_mean', 'T2_roll_mean', 'T1_roll_std', 'T2_roll_std', 'dT1_dt', 'dT2_dt', 'Delta_T'],
    'Gas': ['CO_PPM', 'CO_PPM_roll_mean', 'CO_PPM_roll_std', 'dCO_dt'],
    'Vibration': ['Vib_RMS', 'Vib_Peak', 'Vib_Freq', 'Vib_RMS_roll_mean', 'Vib_Peak_roll_mean', 'Vib_Freq_roll_mean',
                  'Vib_RMS_roll_std', 'Vib_Peak_roll_std', 'Vib_Freq_roll_std', 'dVib_RMS_dt', 'dVib_Peak_dt', 'dVib_Freq_dt']
}

# Trust Score Weights (Aggregation)
WEIGHTS = {
    'Voltage': 0.40,       # Average of Cell1, Cell2, Cell3, Cell4 trust
    'Temperature': 0.20,
    'Current': 0.15,
    'Gas': 0.15,
    'Vibration': 0.10
}

# Training Hyperparameters
AE_EPOCHS = 100
AE_BATCH_SIZE = 256
AE_LR = 0.001
AE_LATENT_DIM = 16
EARLY_STOPPING_PATIENCE = 10
