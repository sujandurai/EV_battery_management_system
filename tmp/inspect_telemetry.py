import pandas as pd
import numpy as np

csv_path = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
df = pd.read_csv(csv_path)

print(f"Columns: {list(df.columns)}")
print(f"Total Rows: {len(df)}")
print("\nUnique Fault_Name counts:")
print(df["Fault_Name"].value_counts())

print("\nMean sensor values by Fault_Name:")
group_cols = ["Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Vib_RMS"]
means = df.groupby("Fault_Name")[group_cols].mean()
print(means.to_string())

print("\nMax sensor values by Fault_Name:")
maxs = df.groupby("Fault_Name")[group_cols].max()
print(maxs.to_string())
