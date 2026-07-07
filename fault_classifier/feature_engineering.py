import pandas as pd
import numpy as np
from fault_classifier.config import STATUS_VALUES

def compute_physical_features(df_input):
    """
    Computes derived cell parameters, time-derivatives, rolling stats, 
    and status one-hot encodings. Works on both batch files and sliding history buffers.
    """
    df = df_input.copy().reset_index(drop=True)
    
    # 1. Physics derivations
    df['Pack_V'] = df['Cell1'] + df['Cell2'] + df['Cell3'] + df['Cell4']
    df['Min_Cell_V'] = df[['Cell1', 'Cell2', 'Cell3', 'Cell4']].min(axis=1)
    df['Max_Cell_V'] = df[['Cell1', 'Cell2', 'Cell3', 'Cell4']].max(axis=1)
    df['Cell_Imbalance'] = df['Max_Cell_V'] - df['Min_Cell_V']
    df['Delta_T'] = df['T1'] - df['T2']
    
    # 2. Time delta (avoid dt=0 or NaNs)
    if 'time' in df.columns:
        dt = df['time'].diff().replace(0, np.nan).bfill().fillna(1.0)
    else:
        dt = pd.Series([1.0] * len(df))
        
    # 3. Time Derivatives
    for i in range(1, 5):
        df[f'dV{i}_dt'] = df[f'Cell{i}'].diff() / dt
        df[f'dV{i}_dt'] = df[f'dV{i}_dt'].bfill().fillna(0.0)
        
    for i in range(1, 3):
        df[f'dT{i}_dt'] = df[f'T{i}'].diff() / dt
        df[f'dT{i}_dt'] = df[f'dT{i}_dt'].bfill().fillna(0.0)
        
    df['dCO_dt'] = df['CO_PPM'].diff() / dt
    df['dCO_dt'] = df['dCO_dt'].bfill().fillna(0.0)
    
    df['dI_dt'] = df['Current'].diff() / dt
    df['dI_dt'] = df['dI_dt'].bfill().fillna(0.0)
    
    df['dImbalance_dt'] = df['Cell_Imbalance'].diff() / dt
    df['dImbalance_dt'] = df['dImbalance_dt'].bfill().fillna(0.0)
    
    df['dSoC_dt'] = df['SoC'].diff() / dt
    df['dSoC_dt'] = df['dSoC_dt'].bfill().fillna(0.0)
    
    # 4. Rolling statistics over 10-sample window
    df['V_rolling_std'] = df['Pack_V'].rolling(window=10, min_periods=1).std().fillna(0.0)
    df['T_rolling_mean'] = df['T1'].rolling(window=10, min_periods=1).mean().fillna(df['T1'])
    
    # 5. One-Hot encoding of Status (CHARGING, DISCHARGING, IDLE)
    for val in STATUS_VALUES:
        col_name = f"Status_{val}"
        if 'Status' in df.columns:
            df[col_name] = (df['Status'] == val).astype(float)
        else:
            df[col_name] = 0.0
            
    return df
