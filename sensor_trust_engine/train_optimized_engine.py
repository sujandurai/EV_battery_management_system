import os
import time
import json
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
import numpy as np
import pandas as pd
import joblib
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, RobustScaler
from sklearn.ensemble import IsolationForest
from sklearn.metrics import precision_recall_fscore_support, roc_auc_score, confusion_matrix

from sensor_trust_engine.config import (
    DATASET_PATH, SCALER_PATH, AUTOENCODER_PATH_PTH, AUTOENCODER_PATH_ONNX,
    ISOLATION_FOREST_PATH, TRUST_THRESHOLDS_PATH, DETECTOR_CONFIG_PATH,
    ENGINEERED_FEATURES, REDUCED_FEATURES, SENSOR_GROUPS, WEIGHTS
)
from feature_engineering import compute_features
from train_autoencoder import Autoencoder

def get_feature_weights(feature_list):
    """
    Computes a weight vector of shape (len(feature_list),) mapping physical sensor
    weights to individual features.
    """
    weights = np.zeros(len(feature_list))
    for sensor, features in SENSOR_GROUPS.items():
        exist_feats = [f for f in features if f in feature_list]
        if not exist_feats:
            continue
            
        if 'Cell' in sensor:
            w_group = 0.10 # 4 cells = 40% voltage weight
        elif sensor == 'Temperature':
            w_group = WEIGHTS['Temperature']
        elif sensor == 'Current':
            w_group = WEIGHTS['Current']
        elif sensor == 'Gas':
            w_group = WEIGHTS['Gas']
        elif sensor == 'Vibration':
            w_group = WEIGHTS['Vibration']
        else:
            w_group = 0.0
            
        w_feat = w_group / len(exist_feats)
        for f in exist_feats:
            idx = feature_list.index(f)
            weights[idx] = w_feat
            
    # Normalize to sum to 1.0
    weights = weights / np.sum(weights)
    return weights

def train_ae_model(X_train, X_val, input_dim, latent_dim, epochs=40, device="cpu"):
    """
    Trains a PyTorch Autoencoder on the healthy training set.
    """
    train_tensor = torch.tensor(X_train, dtype=torch.float32)
    val_tensor = torch.tensor(X_val, dtype=torch.float32)
    
    loader = DataLoader(TensorDataset(train_tensor), batch_size=256, shuffle=True)
    
    model = Autoencoder(input_dim=input_dim, latent_dim=latent_dim).to(device)
    criterion = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    
    best_loss = float('inf')
    best_state = None
    patience = 5
    no_improve = 0
    
    for epoch in range(epochs):
        model.train()
        for batch in loader:
            x_batch = batch[0].to(device)
            optimizer.zero_grad()
            outputs, _ = model(x_batch)
            loss = criterion(outputs, x_batch)
            loss.backward()
            optimizer.step()
            
        # Validation
        model.eval()
        with torch.no_grad():
            val_outputs, _ = model(val_tensor.to(device))
            val_loss = criterion(val_outputs, val_tensor.to(device)).item()
            
        if val_loss < best_loss:
            best_loss = val_loss
            best_state = model.state_dict()
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break
                
    model.load_state_dict(best_state)
    return model, best_loss

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    
    print("=== 1. System Configuration & GPU Detection ===")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")
    
    print("\n=== 2. Loading & Preprocessing Contiguous Battery Files ===")
    import glob
    file_pattern = r"d:\battery 11\generated_battery_dataset\regular_alt_batteries\battery*.csv"
    files = glob.glob(file_pattern)
    
    all_train_h = []
    all_val_h = []
    all_val_f = []
    
    # Process each file to compute features on contiguous chronological data
    for f_idx, f_path in enumerate(files):
        print(f"Processing {os.path.basename(f_path)}...")
        raw_file_df = pd.read_csv(f_path)
        if 'start_time' in raw_file_df.columns:
            raw_file_df = raw_file_df.drop(columns=['start_time'])
            
        # Compute features on the entire continuous file
        df_feat = compute_features(raw_file_df).fillna(0.0)
        
        # Identify segment boundaries and warm-up masks
        raw_file_df['is_boundary'] = raw_file_df['Fault_Name'] != raw_file_df['Fault_Name'].shift()
        
        mask_warmup = np.zeros(len(raw_file_df), dtype=bool)
        boundary_indices = raw_file_df[raw_file_df['is_boundary']].index
        for idx in boundary_indices:
            mask_warmup[idx:min(idx+9, len(raw_file_df))] = True
        mask_warmup[0:min(9, len(raw_file_df))] = True
        
        # Filter healthy and faulty data excluding warmup
        is_healthy = (raw_file_df['Fault_Name'] == 'NORMAL').values & (~mask_warmup)
        is_faulty = (raw_file_df['Fault_Name'] != 'NORMAL').values & (~mask_warmup)
        
        healthy_feat = df_feat[is_healthy].copy()
        if len(healthy_feat) > 0:
            split_idx = int(len(healthy_feat) * 0.8)
            if split_idx > 0:
                all_train_h.append(healthy_feat.iloc[:split_idx])
            if len(healthy_feat) - split_idx > 0:
                all_val_h.append(healthy_feat.iloc[split_idx:])
                
        faulty_feat = df_feat[is_faulty].copy()
        if len(faulty_feat) > 0:
            all_val_f.append(faulty_feat)
                
    # Concatenate all segments
    df_train_h = pd.concat(all_train_h, ignore_index=True)
    if len(df_train_h) > 40000:
        np.random.seed(42)
        df_train_h = df_train_h.sample(n=40000, random_state=42).reset_index(drop=True)
    df_val_h = pd.concat(all_val_h, ignore_index=True)
    
    # Validation faulty data
    df_val_f_all = pd.concat(all_val_f, ignore_index=True)
    # Downsample validation faulty pool to match size of validation healthy pool for balanced metrics
    np.random.seed(42)
    idx_f_sub = np.random.choice(len(df_val_f_all), size=len(df_val_h), replace=False)
    df_val_f = df_val_f_all.iloc[idx_f_sub].reset_index(drop=True)
    
    print(f"Total training healthy samples: {len(df_train_h)}")
    print(f"Total validation healthy samples: {len(df_val_h)}")
    print(f"Total validation faulty samples: {len(df_val_f)}")
    
    # Validation labels (0 = Healthy, 1 = Anomaly)
    y_val = np.array([0] * len(df_val_h) + [1] * len(df_val_f))
    
    # Grid Search Space Parameters
    scalers = {'standard': StandardScaler, 'robust': RobustScaler}
    feature_sets = {'full': ENGINEERED_FEATURES, 'reduced': REDUCED_FEATURES}
    bottlenecks = [16, 24, 32]
    losses = ['unweighted', 'weighted']
    
    # Isolation Forest tuning parameters (subselected for speed)
    if_n_estimators = [100, 200, 300]
    if_contaminations = [0.01, 0.02, 0.03, 0.05]
    if_max_features = [0.7, 1.0]
    if_max_samples = ['auto', 0.8, 0.9]
    
    threshold_strategies = [
        'mu_2.5_sigma', 'mu_3_sigma', 'mu_3.5_sigma',
        'p95', 'p97', 'p99'
    ]
    
    best_overall_score = -1.0
    best_fpr = 1.0
    best_config = None
    best_scaler_model = None
    best_ae_model = None
    best_if_model = None
    best_thresholds_json = None
    
    print("\n=== 3. Starting Combinatorial Grid Search ===")
    total_combos = len(scalers) * len(feature_sets) * len(bottlenecks)
    combo_count = 0
    
    for s_name, ScalerClass in scalers.items():
        for f_name, f_list in feature_sets.items():
            for latent_dim in bottlenecks:
                combo_count += 1
                print(f"\n[Combo {combo_count}/{total_combos}] Scaler: {s_name} | Features: {f_name} | Latent: {latent_dim}")
                
                # Fit scaler
                scaler = ScalerClass()
                X_train_h = scaler.fit_transform(df_train_h[f_list])
                X_val_h = scaler.transform(df_val_h[f_list])
                X_val_f = scaler.transform(df_val_f[f_list])
                X_val = np.concatenate([X_val_h, X_val_f], axis=0)
                
                # Train Autoencoder
                ae, val_loss = train_ae_model(
                    X_train_h, X_val_h, input_dim=len(f_list),
                    latent_dim=latent_dim, epochs=40, device=device
                )
                
                # Run evaluation passes on PyTorch
                ae.eval()
                train_tensor = torch.tensor(X_train_h, dtype=torch.float32).to(device)
                val_tensor = torch.tensor(X_val, dtype=torch.float32).to(device)
                val_healthy_tensor = torch.tensor(X_val_h, dtype=torch.float32).to(device)
                
                with torch.no_grad():
                    train_recon, train_latent = ae(train_tensor)
                    train_recon = train_recon.cpu().numpy()
                    train_latent = train_latent.cpu().numpy()
                    
                    val_recon, val_latent = ae(val_tensor)
                    val_recon = val_recon.cpu().numpy()
                    val_latent = val_latent.cpu().numpy()
                    
                    val_h_recon, _ = ae(val_healthy_tensor)
                    val_h_recon = val_h_recon.cpu().numpy()
                    
                # Compute raw error vectors
                train_errors = (X_train_h - train_recon) ** 2
                val_errors = (X_val - val_recon) ** 2
                val_h_errors = (X_val_h - val_h_recon) ** 2
                
                # Evaluate both weighted and unweighted reconstruction loss
                f_weights = get_feature_weights(f_list)
                
                for loss_type in losses:
                    if loss_type == 'weighted':
                        train_mse = np.sum(f_weights * train_errors, axis=1)
                        val_mse = np.sum(f_weights * val_errors, axis=1)
                        val_h_mse = np.sum(f_weights * val_h_errors, axis=1)
                    else:
                        train_mse = np.mean(train_errors, axis=1)
                        val_mse = np.mean(val_errors, axis=1)
                        val_h_mse = np.mean(val_h_errors, axis=1)
                        
                    # ------------------------------------------------
                    # Strategy 1: Threshold Optimization
                    # ------------------------------------------------
                    for th_strategy in threshold_strategies:
                        # Compute threshold
                        if th_strategy == 'mu_2.5_sigma':
                            th = float(np.mean(train_mse) + 2.5 * np.std(train_mse))
                        elif th_strategy == 'mu_3_sigma':
                            th = float(np.mean(train_mse) + 3.0 * np.std(train_mse))
                        elif th_strategy == 'mu_3_sigma':
                            th = float(np.mean(train_mse) + 3.5 * np.std(train_mse))
                        elif th_strategy == 'p95':
                            th = float(np.percentile(train_mse, 95))
                        elif th_strategy == 'p97':
                            th = float(np.percentile(train_mse, 97))
                        elif th_strategy == 'p99':
                            th = float(np.percentile(train_mse, 99))
                            
                        # Predict
                        y_pred = (val_mse > th).astype(int)
                        # Metrics
                        prec, rec, f1, _ = precision_recall_fscore_support(y_val, y_pred, average='binary', zero_division=0)
                        roc = roc_auc_score(y_val, val_mse)
                        cm = confusion_matrix(y_val, y_pred)
                        tn, fp, fn, tp = cm.ravel()
                        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
                        
                        # Store config if F1-score is higher
                        # Objective priority: 1. F1, 2. lowest FPR, 3. ROC
                        if f1 > best_overall_score or (abs(f1 - best_overall_score) < 1e-4 and fpr < best_fpr):
                            best_overall_score = f1
                            best_fpr = fpr
                            best_config = {
                                'strategy': 1,
                                'scaler_type': s_name,
                                'feature_set': f_name,
                                'latent_dim': latent_dim,
                                'loss_type': loss_type,
                                'threshold_strategy': th_strategy,
                                'threshold': th,
                                'f1': f1,
                                'fpr': fpr,
                                'roc': roc
                            }
                            best_scaler_model = scaler
                            best_ae_model = ae
                            best_if_model = None
                            
                    # ------------------------------------------------
                    # Strategy 2 & 3: Isolation Forest Tuning
                    # ------------------------------------------------
                    # To keep tuning fast, subselect random hyperparameters (Random Search style inside Grid)
                    import random
                    for _ in range(3): # sample 3 random forest hyperparameter sets per AE
                        n_est = random.choice(if_n_estimators)
                        cont = random.choice(if_contaminations)
                        m_feat = random.choice(if_max_features)
                        m_samp = random.choice(if_max_samples)
                        
                        # Strategy 2: Latent Space
                        if_model_lat = IsolationForest(
                            n_estimators=n_est, contamination=cont,
                            max_features=m_feat, max_samples=m_samp,
                            random_state=42, n_jobs=-1
                        )
                        if_model_lat.fit(train_latent)
                        y_pred_s2 = (if_model_lat.predict(val_latent) == -1).astype(int)
                        scores_s2 = -if_model_lat.score_samples(val_latent)
                        
                        prec, rec, f1, _ = precision_recall_fscore_support(y_val, y_pred_s2, average='binary', zero_division=0)
                        roc = roc_auc_score(y_val, scores_s2)
                        tn, fp, fn, tp = confusion_matrix(y_val, y_pred_s2).ravel()
                        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
                        
                        if f1 > best_overall_score or (abs(f1 - best_overall_score) < 1e-4 and fpr < best_fpr):
                            best_overall_score = f1
                            best_fpr = fpr
                            best_config = {
                                'strategy': 2,
                                'scaler_type': s_name,
                                'feature_set': f_name,
                                'latent_dim': latent_dim,
                                'loss_type': loss_type,
                                'n_estimators': int(n_est),
                                'contamination': float(cont),
                                'max_features': float(m_feat) if isinstance(m_feat, float) else m_feat,
                                'max_samples': float(m_samp) if isinstance(m_samp, float) else m_samp,
                                'f1': f1,
                                'fpr': fpr,
                                'roc': roc
                            }
                            best_scaler_model = scaler
                            best_ae_model = ae
                            best_if_model = if_model_lat
                            
                        # Strategy 3: Reconstruction Error Vectors
                        if_model_err = IsolationForest(
                            n_estimators=n_est, contamination=cont,
                            max_features=m_feat, max_samples=m_samp,
                            random_state=42, n_jobs=-1
                        )
                        if_model_err.fit(train_errors)
                        y_pred_s3 = (if_model_err.predict(val_errors) == -1).astype(int)
                        scores_s3 = -if_model_err.score_samples(val_errors)
                        
                        prec, rec, f1, _ = precision_recall_fscore_support(y_val, y_pred_s3, average='binary', zero_division=0)
                        roc = roc_auc_score(y_val, scores_s3)
                        tn, fp, fn, tp = confusion_matrix(y_val, y_pred_s3).ravel()
                        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
                        
                        if f1 > best_overall_score or (abs(f1 - best_overall_score) < 1e-4 and fpr < best_fpr):
                            best_overall_score = f1
                            best_fpr = fpr
                            best_config = {
                                'strategy': 3,
                                'scaler_type': s_name,
                                'feature_set': f_name,
                                'latent_dim': latent_dim,
                                'loss_type': loss_type,
                                'n_estimators': int(n_est),
                                'contamination': float(cont),
                                'max_features': float(m_feat) if isinstance(m_feat, float) else m_feat,
                                'max_samples': float(m_samp) if isinstance(m_samp, float) else m_samp,
                                'f1': f1,
                                'fpr': fpr,
                                'roc': roc
                            }
                            best_scaler_model = scaler
                            best_ae_model = ae
                            best_if_model = if_model_err

    print("\n=== 4. Optimization Search Completed ===")
    print("Winning Configuration Details:")
    print(json.dumps(best_config, indent=4))
    
    # Save Scaler
    print(f"Saving winning Scaler to {SCALER_PATH}...")
    joblib.dump(best_scaler_model, SCALER_PATH)
    
    # Save best PyTorch state dict
    print(f"Saving winning Autoencoder PyTorch model to {AUTOENCODER_PATH_PTH}...")
    torch.save(best_ae_model.state_dict(), AUTOENCODER_PATH_PTH)
    
    # Export ONNX
    print(f"Exporting winning Autoencoder to ONNX format at {AUTOENCODER_PATH_ONNX}...")
    dummy_input = torch.randn(1, len(feature_sets[best_config['feature_set']]), device=device)
    torch.onnx.export(
        best_ae_model,
        dummy_input,
        AUTOENCODER_PATH_ONNX,
        export_params=True,
        opset_version=12,
        do_constant_folding=True,
        input_names=['input_features'],
        output_names=['reconstructed_features', 'latent_features'],
        dynamic_axes={
            'input_features': {0: 'batch_size'},
            'reconstructed_features': {0: 'batch_size'},
            'latent_features': {0: 'batch_size'}
        }
    )
    
    # Save Isolation Forest if Strategy 2 or 3 won
    if best_config['strategy'] in [2, 3]:
        print(f"Saving winning Isolation Forest to {ISOLATION_FOREST_PATH}...")
        joblib.dump(best_if_model, ISOLATION_FOREST_PATH)
        
    # Recalculate and save adaptive thresholds for the winning Scaler + AE
    best_ae_model.eval()
    f_list = feature_sets[best_config['feature_set']]
    X_train_h = best_scaler_model.transform(df_train_h[f_list])
    train_tensor = torch.tensor(X_train_h, dtype=torch.float32).to(device)
    with torch.no_grad():
        recon_scaled, _ = best_ae_model(train_tensor)
        recon_scaled = recon_scaled.cpu().numpy()
        
    train_errors = (X_train_h - recon_scaled) ** 2
    
    # Save thresholds matching the chosen threshold strategy
    thresholds = {}
    for sensor, features in SENSOR_GROUPS.items():
        exist_feats = [f for f in features if f in f_list]
        indices = [f_list.index(f) for f in exist_feats]
        
        sensor_mse = np.mean(train_errors[:, indices], axis=1)
        mu = float(np.mean(sensor_mse))
        sigma = float(np.std(sensor_mse))
        
        # Adaptive thresholds
        th_strategy = best_config.get('threshold_strategy', 'mu_3_sigma')
        if th_strategy == 'mu_2.5_sigma':
            tau = mu + 2.5 * sigma
        elif th_strategy == 'mu_3_sigma':
            tau = mu + 3.0 * sigma
        elif th_strategy == 'mu_3.5_sigma':
            tau = mu + 3.5 * sigma
        elif th_strategy == 'p95':
            tau = float(np.percentile(sensor_mse, 95))
        elif th_strategy == 'p97':
            tau = float(np.percentile(sensor_mse, 97))
        elif th_strategy == 'p99':
            tau = float(np.percentile(sensor_mse, 99))
            
        thresholds[sensor] = {
            'mean': mu,
            'std': sigma,
            'threshold': tau
        }
        
    with open(TRUST_THRESHOLDS_PATH, "w") as f:
        json.dump(thresholds, f, indent=4)
    print(f"Saved adaptive thresholds to {TRUST_THRESHOLDS_PATH}")
    
    # Save detector config
    with open(DETECTOR_CONFIG_PATH, "w") as f:
        json.dump(best_config, f, indent=4)
    print(f"Saved best detector configuration to {DETECTOR_CONFIG_PATH}")
    
    print("\nOptimization and training successfully completed!")

if __name__ == "__main__":
    main()
