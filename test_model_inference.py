import os
import sys
import numpy as np

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fault_classifier.inference import FaultClassificationEngine

print("Attempting to load FaultClassificationEngine...")
engine = FaultClassificationEngine()
print("Success!")

# Let's feed 20 samples (the default sequence length) to warm up the LSTM
for i in range(21):
    row_dict = {
        "Cell1": 3.82,
        "Cell2": 3.81,
        "Cell3": 3.80,
        "Cell4": 3.82,
        "T1": 34.2,
        "T2": 34.1,
        "Current": -1.5,
        "CO_PPM": 5.0,
        "Vib_RMS": 0.03,
        "Vib_Peak": 0.04,
        "Vib_Freq": 50.0,
        "SoC": 68.0,
        "Status": "DISCHARGING"
    }
    engine.add_row(row_dict)

print("Running predict...")
res = engine.predict()
print("Prediction Result:")
print(res)
