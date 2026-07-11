import pandas as pd
df = pd.read_csv(r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv")
stuck_df = df[df["Current"] == -4.892]
print(f"Total rows where Current is exactly -4.892: {len(stuck_df)}")
print("Fault transitions in stuck current rows:")
print(stuck_df["Fault_Name"].value_counts())
# Find where this stuck current block starts and ends in the index
idx = stuck_df.index
print(f"Starts at index {idx[0]} and ends at index {idx[-1]}")
# Print the rows immediately after the stuck current block ends
print("\nRows after stuck current block:")
print(df.iloc[idx[-1]+1 : idx[-1]+10][["time", "Cell1", "Cell2", "Cell3", "Cell4", "T1", "T2", "Current", "CO_PPM", "Fault_Name"]].to_string())
