import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
print("Fault Name Value Counts:")
print(df["Fault_Name"].value_counts())
print("\nFirst 500 rows Fault Name Value Counts:")
print(df.iloc[:500]["Fault_Name"].value_counts())
