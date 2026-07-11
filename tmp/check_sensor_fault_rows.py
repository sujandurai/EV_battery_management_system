import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
sf_df = df[df["Fault_Name"] == "SENSOR_FAULT"]
print(f"Total SENSOR_FAULT rows: {len(sf_df)}")
print(sf_df[["time", "Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Vib_RMS"]].head(20).to_string())
