import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
norm_high = df[(df["Fault_Name"] == "NORMAL") & (df["T1"] > 45)]
print(f"Total NORMAL rows with T1 > 45: {len(norm_high)}")
print(norm_high[["time", "Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Fault_Name"]].head(20).to_string())
