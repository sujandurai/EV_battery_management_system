import pandas as pd
import numpy as np
from sensor_trust_engine.config import RAW_SENSORS, ENGINEERED_FEATURES

def compute_features(df_input):
    """
    Computes derived physical values, time derivatives, and rolling statistics
    on the input DataFrame. Works for both batch and streaming history buffers.
    """
    df = df_input.copy().reset_index(drop=True)
    
    # 1. Derived physics features
    df['Pack_V'] = df['Cell1'] + df['Cell2'] + df['Cell3'] + df['Cell4']
    df['Min_Cell_V'] = df[['Cell1', 'Cell2', 'Cell3', 'Cell4']].min(axis=1)
    df['Max_Cell_V'] = df[['Cell1', 'Cell2', 'Cell3', 'Cell4']].max(axis=1)
    df['Cell_Imbalance'] = df['Max_Cell_V'] - df['Min_Cell_V']
    df['Delta_T'] = df['T1'] - df['T2']
    
    # 2. Time differences (protect against dt=0 or NaNs)
    if 'time' in df.columns:
        dt = df['time'].diff().replace(0, np.nan)
        dt = dt.bfill().fillna(1.0)
    else:
        dt = pd.Series([1.0] * len(df))
        
    # 3. Derivatives
    # Voltages
    for i in range(1, 5):
        df[f'dV{i}_dt'] = df[f'Cell{i}'].diff() / dt
        df[f'dV{i}_dt'] = df[f'dV{i}_dt'].bfill().fillna(0.0)
        
    # Temperatures
    for i in range(1, 3):
        df[f'dT{i}_dt'] = df[f'T{i}'].diff() / dt
        df[f'dT{i}_dt'] = df[f'dT{i}_dt'].bfill().fillna(0.0)
        
    # Current, Gas, and Vibrations
    df['dI_dt'] = df['Current'].diff() / dt
    df['dI_dt'] = df['dI_dt'].bfill().fillna(0.0)
    
    df['dCO_dt'] = df['CO_PPM'].diff() / dt
    df['dCO_dt'] = df['dCO_dt'].bfill().fillna(0.0)
    
    for v_feat in ['Vib_RMS', 'Vib_Peak', 'Vib_Freq']:
        df[f'd{v_feat}_dt'] = df[v_feat].diff() / dt
        df[f'd{v_feat}_dt'] = df[f'd{v_feat}_dt'].bfill().fillna(0.0)
        
    # 4. Rolling statistics (10-sample windows)
    for col in RAW_SENSORS:
        # Rolling Mean
        df[f'{col}_roll_mean'] = df[col].rolling(window=10, min_periods=1).mean()
        df[f'{col}_roll_mean'] = df[f'{col}_roll_mean'].fillna(df[col])
        
        # Rolling Standard Deviation
        df[f'{col}_roll_std'] = df[col].rolling(window=10, min_periods=1).std().fillna(0.0)
        
    # 5. Reorder columns to match config exactly
    return df[ENGINEERED_FEATURES]
