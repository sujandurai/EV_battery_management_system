import os
import sys
import time
import re
import warnings
warnings.filterwarnings("ignore")  # Suppress sklearn version warnings

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Check if serial module is available for real-time mode
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


def find_arduino_port():
    if not SERIAL_AVAILABLE:
        return None
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if "arduino" in p.description.lower() or "usb serial" in p.description.lower() or "ch340" in p.description.lower():
            return p.device
    if ports:
        return ports[0].device
    return None


def parse_serial_line(line):
    if "C1:" not in line:
        return None
    try:
        def get_float(pattern, text):
            match = re.search(pattern, text)
            return float(match.group(1)) if match else 0.0

        c1 = get_float(r"C1:\s*([\d\.\-]+)", line)
        c2 = get_float(r"C2:\s*([\d\.\-]+)", line)
        c3 = get_float(r"C3:\s*([\d\.\-]+)", line)
        c4 = get_float(r"C4:\s*([\d\.\-]+)", line)
        t1 = get_float(r"T1:\s*([\d\.\-]+)", line) if "T1: ERR" not in line else 0.0
        t2 = get_float(r"T2:\s*([\d\.\-]+)", line) if "T2: ERR" not in line else 0.0
        current = get_float(r"Amps:\s*([\d\.\-]+)", line)
        gas     = get_float(r"CO:\s*([\d\.\-]+)", line)
        vib     = get_float(r"Vib:\s*([\d\.\-]+)", line)

        avg_v = (c1 + c2 + c3 + c4) / 4.0
        soc   = max(0.0, min(100.0, (avg_v - 3.0) / 1.2 * 100.0))
        status = "IDLE"
        if current >  0.15: status = "CHARGING"
        elif current < -0.15: status = "DISCHARGING"

        return {
            "Cell1": c1, "Cell2": c2, "Cell3": c3, "Cell4": c4,
            "T1": t1, "T2": t2, "Current": current,
            "CO_PPM": gas, "Vib_RMS": vib,
            "Vib_Peak": vib * 1.414, "Vib_Freq": 25.0 if vib > 0.01 else 0.0,
            "SoC": soc, "Status": status
        }
    except Exception:
        return None


def run_real_time_mode():
    import numpy as np
    import pandas as pd
    from fault_classifier.inference import FaultClassificationEngine

    print("\n" + "=" * 65)
    print("        REAL-TIME TELEMETRY DIAGNOSTIC LOOP")
    print("=" * 65)

    ser  = None
    port = find_arduino_port()
    if port and SERIAL_AVAILABLE:
        try:
            print(f"[SERIAL] Connecting to {port} at 115200 baud...")
            ser = serial.Serial(port, 115200, timeout=1.0)
            print("[SERIAL] Connected! Listening for live hardware telemetry...")
        except Exception as e:
            print(f"[SERIAL ERR] {e} — falling back to CSV simulator.")

    print("[INIT] Loading ML engine (please wait)...")
    engine = FaultClassificationEngine()

    df = None
    row_idx = 0
    if not ser:
        default_csv = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
        if os.path.exists(default_csv):
            print(f"[SIMULATOR] No hardware detected. Streaming '{default_csv}' at 2 Hz...")
            df = pd.read_csv(default_csv)
        else:
            print(f"[ERROR] CSV not found: {default_csv}")
            return

    print("\nPress Ctrl+C to stop.\n")
    engine.history_buffer = []

    while True:
        try:
            row_dict = None
            gt_lbl   = ""

            if ser:
                if ser.in_waiting > 0:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        row_dict = parse_serial_line(line)
                        if not row_dict:
                            print(f"[Arduino] {line}")
                            continue
                else:
                    time.sleep(0.01)
                    continue
            else:
                time.sleep(0.5)
                row = df.iloc[row_idx % len(df)]
                row_dict = {
                    "Cell1": float(row["Cell1"]), "Cell2": float(row["Cell2"]),
                    "Cell3": float(row["Cell3"]), "Cell4": float(row["Cell4"]),
                    "T1": float(row["T1"]),       "T2": float(row["T2"]),
                    "Current": float(row["Current"]),
                    "CO_PPM":  float(row["CO_PPM"]),
                    "Vib_RMS": float(row["Vib_RMS"]),
                    "Vib_Peak":float(row["Vib_Peak"]),
                    "Vib_Freq":float(row["Vib_Freq"]),
                    "SoC":     float(row["SoC"]),
                    "Status":  str(row["Status"])
                }
                gt_lbl = f" | GT: {row.get('Fault_Name', 'NORMAL')}"
                row_idx += 1

            if row_dict:
                engine.history_buffer.append(row_dict)
                if len(engine.history_buffer) > engine.sequence_length:
                    engine.history_buffer.pop(0)

                res          = engine.predict()
                pred         = res["prediction"]
                prob         = res["probability"]
                trust        = int(res["trust_diagnostics"]["overall_trust"])

                print("-" * 80)
                print(f"INPUT : C1={row_dict['Cell1']:.3f}V C2={row_dict['Cell2']:.3f}V "
                      f"C3={row_dict['Cell3']:.3f}V C4={row_dict['Cell4']:.3f}V | "
                      f"Curr={row_dict['Current']:.3f}A | T1={row_dict['T1']:.1f}C "
                      f"T2={row_dict['T2']:.1f}C | CO={row_dict['CO_PPM']:.1f}ppm")
                print(f"OUTPUT: {pred} ({prob*100:.1f}% confidence) | Trust={trust}%{gt_lbl}")

        except KeyboardInterrupt:
            print("\n[INFO] Stopped.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(0.5)


def run_csv_mode(csv_path):
    import numpy as np
    import pandas as pd
    from sklearn.metrics import classification_report, accuracy_score
    from fault_classifier.inference import FaultClassificationEngine
    from fault_classifier.config import INPUT_FEATURES, PRIMARY_CLASSES
    from fault_classifier.feature_engineering import compute_physical_features
    from sensor_trust_engine.config import SENSOR_GROUPS, WEIGHTS
    from sensor_trust_engine.feature_engineering import compute_features

    if not os.path.exists(csv_path):
        print(f"[ERROR] File not found: '{csv_path}'")
        return

    print(f"\n[INIT] Loading: {csv_path}...")
    df = pd.read_csv(csv_path)
    print(f"[INIT] {len(df)} rows loaded.")

    print("[INIT] Loading ML engines...")
    engine = FaultClassificationEngine()

    print("[PRE-COMPUTE] Engineering & scaling features...")
    t0 = time.time()
    df_feat_lstm  = compute_physical_features(df).fillna(0.0)
    scaled_lstm   = engine.scaler.transform(df_feat_lstm[INPUT_FEATURES].values).astype("float32")

    feat_list      = engine.trust_engine.feature_list
    df_feat_trust  = compute_features(df).fillna(0.0)
    scaled_trust   = engine.trust_engine.scaler.transform(df_feat_trust[feat_list]).astype("float32")
    print(f"[PRE-COMPUTE] Done in {time.time()-t0:.2f}s — starting inference...\n")

    engine.trust_engine.history_buffer = []
    engine.trust_engine.trust_history  = []
    engine.prob_history = []

    pipeline_preds, ground_truths = [], []
    start = time.time()
    num_rows = len(df)

    for i in range(num_rows):
        row = df.iloc[i]

        # --- Trust Engine ---
        scaled_row = scaled_trust[i:i+1]
        ort_in  = {engine.trust_engine.ort_session.get_inputs()[0].name: scaled_row}
        ort_out = engine.trust_engine.ort_session.run(None, ort_in)
        recon, latent = ort_out[0], ort_out[1]
        feat_err = (scaled_row - recon) ** 2
        feat_err_flat = feat_err[0]

        row_d = row.to_dict()
        engine.trust_engine.history_buffer.append(row_d)
        if len(engine.trust_engine.history_buffer) > 10:
            engine.trust_engine.history_buffer.pop(0)

        phys = {s: 100.0 for s in SENSOR_GROUPS}
        t1_v = float(row_d["T1"]); t2_v = float(row_d["T2"])
        co_v = float(row_d.get("CO_PPM", 0.0))
        is_tr = (t1_v > 150.0 or t2_v > 150.0 or co_v > 10.0)

        if len(engine.trust_engine.history_buffer) >= 10:
            curr_vals = [float(r["Current"]) for r in engine.trust_engine.history_buffer]
            if np.std(curr_vals) < 0.0001 and abs(curr_vals[-1]) > 0.1:
                phys["Current"] = 0.0
            if abs(curr_vals[-1]) > 150.0:
                phys["Current"] = 0.0

        for ci in range(1, 5):
            cn  = f"Cell{ci}"
            cv  = float(row_d[cn])
            if (cv < 0.5 and not is_tr) or cv > 5.0:
                phys[cn] = 0.0
            if len(engine.trust_engine.history_buffer) >= 2:
                if abs(cv - float(engine.trust_engine.history_buffer[-2][cn])) > 1.5:
                    phys[cn] = 0.0

        if t1_v < -40 or t1_v > 250 or t1_v == -127: phys["Temperature"] = 0.0
        if t2_v < -40 or t2_v > 250 or t2_v == -127: phys["Temperature"] = 0.0
        if len(engine.trust_engine.history_buffer) >= 2 and not is_tr:
            pt1 = float(engine.trust_engine.history_buffer[-2]["T1"])
            pt2 = float(engine.trust_engine.history_buffer[-2]["T2"])
            if abs(t1_v - pt1) > 15 or abs(t2_v - pt2) > 15:
                phys["Temperature"] = 0.0

        raw_trusts = {}
        for sensor, feats in SENSOR_GROUPS.items():
            idxs = [feat_list.index(f) for f in feats if f in feat_list]
            if engine.trust_engine.loss_type == "weighted":
                w = engine.trust_engine.feature_weights[idxs]
                w = w / w.sum()
                e_s = float(np.sum(w * feat_err_flat[idxs]))
            else:
                e_s = float(np.mean(feat_err_flat[idxs]))
            tau = engine.trust_engine.thresholds[sensor]["threshold"]
            ts  = 100.0 - 10.0*(e_s/tau) if e_s <= tau else 90.0*np.exp(-2.0*(e_s-tau)/tau)
            raw_trusts[sensor] = min(max(ts, 0.0), phys[sensor])

        engine.trust_engine.trust_history.append(raw_trusts)
        if len(engine.trust_engine.trust_history) > 5:
            engine.trust_engine.trust_history.pop(0)

        smoothed = {}
        if engine.trust_engine.smoothing_strategy == "exponential" and len(engine.trust_engine.trust_history) > 1:
            prev = engine.trust_engine.trust_history[-2]
            for s in raw_trusts:
                smoothed[s] = int(0.70 * prev.get(s, raw_trusts[s]) + 0.30 * raw_trusts[s])
        else:
            for s in raw_trusts:
                smoothed[s] = int(raw_trusts[s])

        for s in smoothed:
            smoothed[s] = 0 if phys.get(s, 100) == 0.0 else max(90, smoothed[s])

        avg_v_trust = np.mean([smoothed[c] for c in ["Cell1","Cell2","Cell3","Cell4"]])
        ot = int(WEIGHTS["Voltage"]*avg_v_trust + WEIGHTS["Temperature"]*smoothed["Temperature"] +
                 WEIGHTS["Current"]*smoothed["Current"] + WEIGHTS["Gas"]*smoothed["Gas"] +
                 WEIGHTS["Vibration"]*smoothed["Vibration"])
        ot = max(0, min(100, ot))

        if engine.trust_engine.winning_strategy in (2, 3):
            inp_if = latent if engine.trust_engine.winning_strategy == 2 else feat_err
            if engine.trust_engine.clf.predict(inp_if)[0] == -1 and any(v == 0.0 for v in phys.values()):
                ot = min(ot, 50)
        if any(v == 0.0 for v in phys.values()):
            ot = min(ot, 50)

        # --- LSTM ---
        if i < engine.sequence_length - 1:
            probs = np.zeros(len(PRIMARY_CLASSES))
            probs[PRIMARY_CLASSES.index("NORMAL")] = 1.0
        else:
            win  = scaled_lstm[i - engine.sequence_length + 1 : i + 1]
            inp  = np.expand_dims(win, 0)
            out  = engine.ort_session.run(None, {engine.ort_session.get_inputs()[0].name: inp})
            logits = out[0][0]
            ex = np.exp(logits - logits.max())
            probs = ex / ex.sum()

        engine.prob_history.append(probs)
        if len(engine.prob_history) > 3: engine.prob_history.pop(0)

        avg_p = np.mean(engine.prob_history, axis=0)
        final_cls = engine.encoder.inverse_transform([int(np.argmax(avg_p))])[0]

        pred = "SENSOR_FAULT" if ot < engine.trust_threshold else final_cls
        gt   = str(row.get("Fault_Name", "N/A"))
        pipeline_preds.append(pred)
        ground_truths.append(gt)

        print(f"[Row {i:4d}] C1={row['Cell1']:.3f}V | Curr={row['Current']:.3f}A | "
              f"T1={row['T1']:.1f}C || Pred={pred:21s} | Trust={ot:3d}% | GT={gt}")

    elapsed = time.time() - start
    print(f"\n[DONE] {num_rows} rows in {elapsed:.2f}s ({num_rows/elapsed:.1f} rows/sec)")

    if "Fault_Name" in df.columns:
        ep = pipeline_preds[engine.sequence_length:]
        eg = ground_truths[engine.sequence_length:]
        acc = accuracy_score(eg, ep)
        print("\n" + "="*70)
        print("  PIPELINE EVALUATION REPORT")
        print("="*70)
        print(f"Overall Accuracy: {acc*100:.2f}%\n")
        print(classification_report(eg, ep, labels=sorted(set(eg)), zero_division=0))


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 65)
    print("        EV Guardian - Diagnostics CLI")
    print("=" * 65)
    try:
        choice = input("Enter choice (1 = Real-Time, 2 = CSV File): ").strip()
    except (KeyboardInterrupt, SystemExit, EOFError):
        choice = '1'

    if choice == '1':
        run_real_time_mode()
    else:
        default_csv = r"C:\Users\Admin\Downloads\qualcomm_test_telemetry.csv"
        try:
            csv_path = input(f"Enter CSV path [Enter = default '{default_csv}']: ").strip()
        except (KeyboardInterrupt, SystemExit, EOFError):
            csv_path = default_csv
        if not csv_path:
            csv_path = default_csv
        run_csv_mode(csv_path)
