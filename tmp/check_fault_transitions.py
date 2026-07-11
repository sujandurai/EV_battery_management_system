import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
print(df.iloc[1550:1600][["time", "Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Fault_Name"]].to_string())
