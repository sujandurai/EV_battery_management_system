import os

# Relative paths pointing to the models subdirectory
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(PACKAGE_DIR, "models")

SCALER_PATH = os.path.join(MODELS_DIR, "feature_scaler.joblib")
AUTOENCODER_PATH_ONNX = os.path.join(MODELS_DIR, "sensor_autoencoder.onnx")
ISOLATION_FOREST_PATH = os.path.join(MODELS_DIR, "isolation_forest.joblib")
TRUST_THRESHOLDS_PATH = os.path.join(MODELS_DIR, "trust_thresholds.json")
DETECTOR_CONFIG_PATH = os.path.join(MODELS_DIR, "detector_config.json")

MODEL_ONNX_PATH = os.path.join(MODELS_DIR, "battery_fault_classifier.onnx")

# Physical sensors
RAW_SENSORS = [
    'Cell1', 'Cell2', 'Cell3', 'Cell4',
    'Current',
    'T1', 'T2',
    'CO_PPM',
    'Vib_RMS', 'Vib_Peak', 'Vib_Freq'
]

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

ROLLING_MEANS = [f"{s}_roll_mean" for s in RAW_SENSORS]
ROLLING_STDS = [f"{s}_roll_std" for s in RAW_SENSORS]

ENGINEERED_FEATURES = RAW_SENSORS + DERIVED_PHYSICS + DERIVATIVES + ROLLING_MEANS + ROLLING_STDS
REDUCED_FEATURES = RAW_SENSORS + DERIVED_PHYSICS + REDUCED_DERIVATIVES + ROLLING_MEANS + ROLLING_STDS

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

WEIGHTS = {
    'Voltage': 0.40,
    'Temperature': 0.20,
    'Current': 0.15,
    'Gas': 0.15,
    'Vibration': 0.10
}
