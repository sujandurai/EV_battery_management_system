import pandas as pd
import numpy as np
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sensor_trust_engine.sensor_trust_engine import SensorTrustEngine

csv_path = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
df = pd.read_csv(csv_path)

engine = SensorTrustEngine()
trusts = []
anomalies = []

for i in range(min(500, len(df))):
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
    res = engine.diagnose_row(row_dict)
    trusts.append(res['overall_trust'])
    anomalies.append(res['anomalous_sensors'])

trusts = np.array(trusts)
print(f"Overall Trust statistics:")
print(f"  Mean: {np.mean(trusts):.2f}")
print(f"  Min:  {np.min(trusts):.2f}")
print(f"  Max:  {np.max(trusts):.2f}")
print(f"  Below 80 count: {np.sum(trusts < 80)} / {len(trusts)}")

# Let's see what are the anomalous sensors
from collections import Counter
c = Counter()
for a in anomalies:
    for s in a:
        c[s] += 1
print("\nMost common anomalous sensors identified:")
for k, v in c.most_common():
    print(f"  {k}: {v}")
