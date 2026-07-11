import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
sf_other = df[(df["Fault_Name"] == "SENSOR_FAULT") & (df["T1"] < 100)]
print(f"Total SENSOR_FAULT rows with T1 < 100: {len(sf_other)}")
if len(sf_other) > 0:
    print(sf_other[["time", "Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Vib_RMS"]].head(20).to_string())
