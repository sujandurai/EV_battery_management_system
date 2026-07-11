"""
EV Guardian - XPC Backend v4.0  (FINAL)
=========================================
Dual ONNX Inference: Anomaly Detection + State-of-Health Estimation
+ WebSocket Broadcast (port 8887)
+ HTTP Diagnostics API (port 8766)
+ SQLite persistence with full schema
+ QNN EP / DirectML / CUDA / CPU provider fallback chain
"""

import time, json, sqlite3, threading, asyncio, queue, os, logging
import numpy as np
import paho.mqtt.client as mqtt
import onnxruntime as ort
import websockets
from aiohttp import web

logging.basicConfig(level=logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER    = "localhost"
MQTT_PORT      = 1883
MQTT_TOPIC     = "ev/sensor/telemetry"
TOPIC_TRUST    = "ev/analytics/trust_status"
TOPIC_PINN_CONTROL = "ev/control/pinn"
TOPIC_DIAGNOSTICS_PRED = "ev/diagnostics/prediction"
DB_FILE        = "ev_telemetry.db"
ANOMALY_MODEL  = "anomaly_model.onnx"
SOH_MODEL      = "models/soh_model.onnx"
PINN_MODEL     = "models/pinn_battery_twin.onnx"
WS_PORT        = 8887
HTTP_PORT      = 8766

CELL_VOLT_MIN  = 2.5
TEMP_CRITICAL  = 60.0
GAS_CRITICAL   = 50.0

_broadcast_q: queue.Queue = queue.Queue(maxsize=500)
_ws_clients: set          = set()
_packet_count  = 0
_anomaly_count = 0

# ── ONNX Sessions ─────────────────────────────────────────────────────────────
def _build_providers() -> list:
    avail = ort.get_available_providers()
    providers = []
    
    # 1. Qualcomm QNN EP (NPU) — Max performance burst mode
    if "QNNExecutionProvider" in avail:
        providers.append((
            "QNNExecutionProvider",
            {
                "backend_path": "QnnHtp.dll",
                "htp_performance_mode": "burst"  # Force NPU into highest speed/low-latency burst mode
            }
        ))
        
    # 2. NVIDIA CUDA EP (GPU) — High performance parameters
    if "CUDAExecutionProvider" in avail:
        providers.append((
            "CUDAExecutionProvider",
            {
                "device_id": 0,
                "arena_extend_strategy": "kNextPowerOfTwo",
                "cudnn_conv_algo_search": "EXHAUSTIVE",
                "do_copy_in_default_stream": True
            }
        ))
        
    # 3. DirectML EP (GPU) — For AMD / Intel / Qualcomm Built-in Graphics
    if "DmlExecutionProvider" in avail:
        providers.append("DmlExecutionProvider")
        
    # 4. Standard CPU fallback
    providers.append("CPUExecutionProvider")
    return providers

def load_session(path: str, tag: str):
    if not os.path.exists(path):
        print(f"[WARN] {tag} model not found: {path}")
        return None, None
    try:
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        
        # Build lists of providers & their configurations
        providers_list = _build_providers()
        provs = []
        prov_opts = []
        for p in providers_list:
            if isinstance(p, tuple):
                provs.append(p[0])
                prov_opts.append(p[1])
            else:
                provs.append(p)
                prov_opts.append({})
                
        sess = ort.InferenceSession(
            path, 
            sess_options=opts, 
            providers=provs, 
            provider_options=prov_opts
        )
        iname = sess.get_inputs()[0].name
        print(f"[ONNX] {tag:8s} | {path}  providers={sess.get_providers()}")
        return sess, iname
    except Exception as e:
        print(f"[ONNX ERR] {tag}: {e}")
        return None, None

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER,
            device_id       TEXT,
            cell_1_v REAL, cell_2_v REAL, cell_3_v REAL, cell_4_v REAL,
            cell_1_t REAL, cell_2_t REAL, cell_3_t REAL, cell_4_t REAL,
            current_a       REAL,
            vibration_g     REAL,
            gas_ppm         REAL,
            is_anomaly      INTEGER DEFAULT 0,
            anomaly_score   REAL    DEFAULT 0.0,
            soh_pct         REAL    DEFAULT 100.0,
            lli_pct         REAL    DEFAULT 0.0,
            lam_pct         REAL    DEFAULT 0.0,
            r_sei_ohms      REAL    DEFAULT 0.0,
            alert_reason    TEXT    DEFAULT '',
            received_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Run dynamic sqlite dynamic migration to add columns if updating older database
    for col, ctype in [("lli_pct", "REAL DEFAULT 0.0"), ("lam_pct", "REAL DEFAULT 0.0"), ("r_sei_ohms", "REAL DEFAULT 0.0")]:
        try:
            conn.execute(f"ALTER TABLE telemetry_logs ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass # Column already exists
    conn.commit(); conn.close()
    print(f"[DB] Ready (Physics-Integrated): '{DB_FILE}'")

def save_to_db(data, is_anomaly, anomaly_score, soh_pct, lli, lam, r_sei, alert_reason):
    try:
        c = data.get("cells", {})
        v = list(c.get("voltage_v", [0]*4)); v += [0.0]*(4-len(v))
        t = list(c.get("temp_c",    [0]*4)); t += [0.0]*(4-len(t))
        p = data.get("pack", {})
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            INSERT INTO telemetry_logs
              (timestamp,device_id,cell_1_v,cell_2_v,cell_3_v,cell_4_v,
               cell_1_t,cell_2_t,cell_3_t,cell_4_t,
               current_a,vibration_g,gas_ppm,
               is_anomaly,anomaly_score,soh_pct,lli_pct,lam_pct,r_sei_ohms,alert_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (data.get("timestamp"), data.get("device_id"),
              v[0],v[1],v[2],v[3], t[0],t[1],t[2],t[3],
              p.get("current_a"), p.get("vibration_g"), p.get("gas_ppm"),
              int(is_anomaly), round(anomaly_score,6),
              round(soh_pct, 2), round(lli, 2), round(lam, 2), round(r_sei, 4), alert_reason))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB ERR] {e}")

# ── Vectorization ─────────────────────────────────────────────────────────────
def vectorize(data) -> np.ndarray:
    c = data.get("cells", {})
    v = list(c.get("voltage_v", [0]*4)); v += [0.0]*(4-len(v))
    t = list(c.get("temp_c", [0]*4))
    p = data.get("pack", {})
    return np.array([[v[0],v[1],v[2],v[3],
                      p.get("current_a",0.0),
                      max(t) if t else 0.0,
                      p.get("vibration_g",0.0),
                      p.get("gas_ppm",0.0)]], dtype=np.float32)

def vectorize_pinn(data) -> np.ndarray:
    c = data.get("cells", {})
    v = list(c.get("voltage_v", [0]*4)); v += [0.0]*(4-len(v))
    t = list(c.get("temp_c", [0]*2)); t += [0.0]*(2-len(t))
    p = data.get("pack", {})
    return np.array([[v[0], v[1], v[2], v[3],
                      p.get("current_a", 0.0),
                      t[0], t[1],
                      p.get("vibration_g", 0.0),
                      p.get("gas_ppm", 0.0)]], dtype=np.float32)

# ── Hard Rules ────────────────────────────────────────────────────────────────
def hard_rules(data):
    v = data.get("cells",{}).get("voltage_v",[])
    t = data.get("cells",{}).get("temp_c",[])
    p = data.get("pack",{})
    r = []
    
    # 1. Voltage Check with Vibration-Correlated Loose Wire Logic (Scenario 3)
    for i,val in enumerate(v):
        if val < CELL_VOLT_MIN:
            vibration = p.get("vibration_g", 0.0)
            # If overall pack shows at least 3 cells are stable (above 3.0V) and vibration is high,
            # it indicates a loose tap connection rather than general cell depletion.
            other_cells_ok = sum(1 for x in v if x > 3.0) >= 3
            if vibration > 0.8 and other_cells_ok:
                r.append(f"LOOSE_BALANCE_WIRE_CELL_{i+1}_VIB({val:.2f}V,vib={vibration:.2f}g)")
            else:
                r.append(f"CELL_{i+1}_VOLT_LOW({val:.2f}V)")
                
    # 2. Over-Temperature with Venting CO Gas Correlation (Scenario 1)
    for i,val in enumerate(t):
        if val > TEMP_CRITICAL:  
            gas = p.get("gas_ppm", 0.0)
            if gas > GAS_CRITICAL:
                r.append(f"CRITICAL_THERMAL_RUNAWAY_LEAK_CELL_{i+1}({val:.1f}C,gas={gas:.1f}ppm)")
            else:
                r.append(f"CELL_{i+1}_TEMP_HIGH({val:.1f}C)")
                
    # 3. Gas Over-limit alone
    if p.get("gas_ppm",0) > GAS_CRITICAL and not any(val > TEMP_CRITICAL for val in t):
         r.append(f"GAS_HIGH({p['gas_ppm']}ppm)")
         
    # 4. Thermal Gradient Hotspot Delta
    valid_temps = [val for val in t if val > -50.0]
    if len(valid_temps) >= 2:
        temp_delta = max(valid_temps) - min(valid_temps)
        if temp_delta > 4.0:
            r.append(f"LOCALIZED_HOTSPOT_ALERT(delta={temp_delta:.1f}C)")
            
    return bool(r), "|".join(r)

# ── ONNX Inference ────────────────────────────────────────────────────────────
_anom_sess = None; _anom_inp = None
_soh_sess  = None; _soh_inp  = None
_pinn_sess = None; _pinn_inp = None
_fault_classifier = None

def run_anomaly(vec: np.ndarray) -> tuple[int, float]:
    if _anom_sess is None: return 1, 0.0
    try:
        label_out, score_out = _anom_sess.run(None, {_anom_inp: vec})
        label = int(label_out[0])
        try:    score = float(score_out[0].get(-1, 0.0))
        except: score = float(np.array(score_out[0]).flat[0])
        return label, score
    except Exception as e:
        print(f"[ANOM ERR] {e}")
        return 1, 0.0

def run_soh(vec: np.ndarray) -> float:
    if _soh_sess is None: return 100.0
    try:
        out = _soh_sess.run(None, {_soh_inp: vec})[0]
        return float(np.clip(np.array(out).flat[0], 0.0, 100.0))
    except Exception as e:
        print(f"[SOH ERR] {e}")
        return 100.0

def run_pinn(vec: np.ndarray) -> tuple[float, float, float]:
    if _pinn_sess is None: return 0.0, 0.0, 0.015
    try:
        out = _pinn_sess.run(None, {_pinn_inp: vec})[0]
        res = np.array(out).flat
        return float(res[0]), float(res[1]), float(res[2])
    except Exception as e:
        print(f"[PINN ERR] {e}")
        return 0.0, 0.0, 0.015

# ── MQTT Callbacks ────────────────────────────────────────────────────────────
def on_message(client, userdata, message):
    global _packet_count, _anomaly_count
    try:
        data = json.loads(message.payload.decode())
        _packet_count += 1

        cells = data.get("cells", {})
        pack  = data.get("pack", {})
        volts = cells.get("voltage_v", [])
        temps = cells.get("temp_c", [])

        vec_8 = vectorize(data)
        vec_pinn = vectorize_pinn(data)

        # Run ONNX models (Anomaly, SOH, and PINN Battery Twin)
        anom_label, anom_score = run_anomaly(vec_8)
        soh_pct                = run_soh(vec_8)
        lli_pct, lam_pct, r_sei = run_pinn(vec_pinn)

        rule_fault, rule_reason = hard_rules(data)

        # ── Fault Classification & Sensor Trust Engine Integration ──
        volt_1 = volts[0] if len(volts) > 0 else 0.0
        volt_2 = volts[1] if len(volts) > 1 else 0.0
        volt_3 = volts[2] if len(volts) > 2 else 0.0
        volt_4 = volts[3] if len(volts) > 3 else 0.0

        temp_1 = temps[0] if len(temps) > 0 else 0.0
        temp_2 = temps[1] if len(temps) > 1 else 0.0

        current_a = pack.get("current_a", 0.0)
        gas_ppm = pack.get("gas_ppm", 0.0)
        vibration_g = pack.get("vibration_g", 0.0)

        # Calculate SoC from cell voltages
        pack_v = volt_1 + volt_2 + volt_3 + volt_4
        soc = float(np.clip((pack_v / 4.0 - 3.0) / 1.2 * 100.0, 0.0, 100.0))

        # Infer status
        if current_a < -0.1:
            status = "DISCHARGING"
        elif current_a > 0.1:
            status = "CHARGING"
        else:
            status = "IDLE"

        # Build row dictionary
        row_dict = {
            "Cell1": volt_1,
            "Cell2": volt_2,
            "Cell3": volt_3,
            "Cell4": volt_4,
            "T1": temp_1,
            "T2": temp_2,
            "Current": current_a,
            "CO_PPM": gas_ppm,
            "Vib_RMS": vibration_g,
            "Vib_Peak": vibration_g * 1.414,
            "Vib_Freq": 50.0,
            "SoC": soc,
            "Status": status
        }

        clf_result = None
        if _fault_classifier is not None:
            _fault_classifier.add_row(row_dict)
            clf_result = _fault_classifier.predict()

        if clf_result is not None:
            trust_diag = clf_result.get("trust_diagnostics", {})
            st = trust_diag.get("sensor_trust", {})
            cell_trust_pct = [
                round(st.get("Cell1", 99.0), 1),
                round(st.get("Cell2", 99.0), 1),
                round(st.get("Cell3", 99.0), 1),
                round(st.get("Cell4", 99.0), 1)
            ]
            overall_trust = round(trust_diag.get("overall_trust", 100.0), 1)
            clf_prediction = clf_result.get("prediction", "NORMAL")
        else:
            cell_trust_pct = [99.0, 99.0, 99.0, 99.0]
            overall_trust = 100.0
            clf_prediction = "NORMAL"

        # Determine anomaly and alert reasoning
        is_anomaly = (clf_prediction != "NORMAL") or (anom_label == -1) or rule_fault
        
        # Build alert reason hierarchy
        if rule_fault:
            alert_reason = rule_reason
        elif clf_prediction == "SENSOR_FAULT" and clf_result is not None:
            trust_diag = clf_result.get("trust_diagnostics", {})
            anom_sens = trust_diag.get("anomalous_sensors", [])
            alert_reason = f"SENSOR_FAULT(Low Trust on {', '.join(anom_sens) if anom_sens else 'unknown'})"
        elif clf_prediction != "NORMAL":
            alert_reason = f"AI_FAULT_{clf_prediction}"
        elif anom_label == -1:
            alert_reason = "MODEL_ANOMALY"
        else:
            alert_reason = ""

        if is_anomaly:
            _anomaly_count += 1

        # Log into SQLite DB
        save_to_db(data, is_anomaly, anom_score, soh_pct, lli_pct, lam_pct, r_sei, alert_reason)

        # Publish trust status back to Gateway (for STM32 LED display)
        trust_status_val = "FAULT" if is_anomaly else "OK"
        trust_msg = json.dumps({"status": trust_status_val})
        try:
            client.publish(TOPIC_TRUST, trust_msg)
        except Exception:
            pass

        # Publish PINN physical control outputs to target gateway/motor controller
        pinn_msg = json.dumps({
            "lli_pct": round(lli_pct, 2),
            "lam_pct": round(lam_pct, 2),
            "r_sei_ohms": round(r_sei, 4)
        })
        try:
            client.publish(TOPIC_PINN_CONTROL, pinn_msg)
        except Exception:
            pass

        # Publish diagnostic ML outcomes for serial bridge display
        pred_msg = json.dumps({
            "prediction": clf_prediction,
            "overall_trust": overall_trust,
            "soh_pct": round(soh_pct, 2),
            "is_anomaly": is_anomaly,
            "complexity_reason": alert_reason
        })
        try:
            client.publish(TOPIC_DIAGNOSTICS_PRED, pred_msg)
        except Exception:
            pass

        # Build WebSocket broadcast frame containing all models outputs
        frame = {
            "type":           "ANOMALY" if is_anomaly else "TELEMETRY",
            "timestamp":      data.get("timestamp"),
            "device_id":      data.get("device_id", "unknown"),
            "voltages":       volts,
            "temperatures":   temps,
            "current_a":      pack.get("current_a",   0.0),
            "vibration_g":    pack.get("vibration_g", 0.0),
            "gas_ppm":        pack.get("gas_ppm",     0.0),
            "is_anomaly":     is_anomaly,
            "anomaly_score":  round(anom_score, 6),
            "soh_pct":        round(soh_pct, 2),
            "lli_pct":        round(lli_pct, 2),
            "lam_pct":        round(lam_pct, 2),
            "r_sei_ohms":      round(r_sei, 4),
            "alert_reason":   alert_reason,
            "packet_count":   _packet_count,
            "anomaly_count":  _anomaly_count,
            "ai_fault_prediction": clf_prediction,
            "overall_trust":  overall_trust,
            "npu_metrics": {
                "sensor_trust_pct":    cell_trust_pct,
                "state_of_health_pct": round(soh_pct, 2),
                "loss_of_lithium_pct": round(lli_pct, 2),
                "loss_of_active_material_pct": round(lam_pct, 2),
                "sei_resistance_ohms": round(r_sei, 4)
            }
        }

        try:    _broadcast_q.put_nowait(json.dumps(frame))
        except queue.Full: pass

        # Console output
        v = [round(x,2) for x in cells.get("voltage_v",[])]
        if is_anomaly:
            print(f"\n{'!'*68}")
            print(f"  *** ANOMALY #{_anomaly_count} | Pkt #{_packet_count} ***")
            print(f"  Reason: {alert_reason}")
            print(f"  Score : {anom_score:.5f}  |  SOH: {soh_pct:.1f}%")
            print(f"  LLI   : {lli_pct:.1f}% | LAM: {lam_pct:.1f}% | R_sei: {r_sei:.4f}Ohm")
            print(f"  Volts : {v}")
            print(f"{'!'*68}\n")
        else:
            print(f"\r[OK] #{_packet_count:05d} V={v} SOH={soh_pct:.1f}% LLI={lli_pct:.1f}% LAM={lam_pct:.1f}% R_sei={r_sei:.4f}Ohm Score={anom_score:.4f} Anom={_anomaly_count}",
                  end="", flush=True)

    except Exception as e:
        print(f"\n[MSG ERR] {e}")

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected. Subscribed to '{MQTT_TOPIC}'")
        client.subscribe(MQTT_TOPIC)
    else:
        print(f"[MQTT] Failed rc={rc}")

# ── WebSocket Server ──────────────────────────────────────────────────────────
async def ws_handler(websocket):
    _ws_clients.add(websocket)
    print(f"\n[WS] Client connected ({len(_ws_clients)} total)")
    try:    await websocket.wait_closed()
    finally:
        _ws_clients.discard(websocket)
        print(f"\n[WS] Client disconnected ({len(_ws_clients)} total)")

async def ws_broadcaster():
    while True:
        try:
            msg = _broadcast_q.get_nowait()
            if _ws_clients:
                await asyncio.gather(*[ws.send(msg) for ws in list(_ws_clients)],
                                     return_exceptions=True)
        except queue.Empty: pass
        await asyncio.sleep(0.01)

async def run_ws():
    print(f"[WS] Server on ws://localhost:{WS_PORT}")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await ws_broadcaster()

# ── HTTP API ──────────────────────────────────────────────────────────────────
async def http_diagnose(request):
    reason = request.rel_url.query.get("reason", "UNKNOWN_FAULT")
    model_name = request.rel_url.query.get("model", "")
    temp_val = request.rel_url.query.get("temperature", "")
    system_prompt = request.rel_url.query.get("system_prompt", "")
    
    # Fetch telemetry context from database
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cell_1_v, cell_2_v, cell_3_v, cell_4_v,
                   cell_1_t, cell_2_t, cell_3_t, cell_4_t,
                   current_a, soh_pct
            FROM telemetry_logs 
            ORDER BY id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()
    except Exception:
        row = None

    if row:
        v1, v2, v3, v4, t1, t2, t3, t4, current_a, soh_pct = row
        # Calculate derived metrics matching typical LiFePO4 aging cycles
        rul = max(0, int((soh_pct - 20) * 12))
        cycles = max(0, int((100 - soh_pct) * 15) + 140)
        
        telemetry_prompt = (
            f"Battery Telemetry:\n\n"
            f"Battery Type: LiFePO4\n\n"
            f"Cell Voltages:\n"
            f"Cell1: {v1:.2f}V\n"
            f"Cell2: {v2:.2f}V\n"
            f"Cell3: {v3:.2f}V\n"
            f"Cell4: {v4:.2f}V\n\n"
            f"Pack Current:\n"
            f"{current_a:.1f}A\n\n"
            f"Temperatures:\n"
            f"Cell1: {t1:.1f}°C\n"
            f"Cell2: {t2:.1f}°C\n"
            f"Cell3: {t3:.1f}°C\n"
            f"Cell4: {t4:.1f}°C\n\n"
            f"SOH Prediction:\n"
            f"{soh_pct:.1f}%\n\n"
            f"RUL Prediction:\n"
            f"{rul} cycles\n\n"
            f"Life Cycle Count:\n"
            f"{cycles} cycles\n\n"
            f"User Question:\n"
            f"{reason}"
        )
    else:
        telemetry_prompt = f"User Question:\n{reason}"

    try:
        from llm_diagnostics import get_diagnosis, OLLAMA_MODEL, SYSTEM_PROMPT
        m = model_name if model_name else OLLAMA_MODEL
        t = float(temp_val) if temp_val else 0.7
        s = system_prompt if system_prompt else SYSTEM_PROMPT
        diag = get_diagnosis(telemetry_prompt, model_name=m, temperature=t, system_prompt=s)
    except Exception as e:
        diag = f"Fault: {reason}. Check sensor connections and restart BMS. Error: {e}"
    return web.Response(
        text=json.dumps({"reason": reason, "diagnosis": diag}),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )

async def http_status(request):
    try:
        conn = sqlite3.connect(DB_FILE)
        total = conn.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0]
        anom  = conn.execute("SELECT COUNT(*) FROM telemetry_logs WHERE is_anomaly=1").fetchone()[0]
        avg_soh = conn.execute("SELECT AVG(soh_pct) FROM telemetry_logs WHERE is_anomaly=0").fetchone()[0] or 100.0
        conn.close()
    except: total=0; anom=0; avg_soh=100.0
    return web.Response(
        text=json.dumps({"status":"running","total_packets":total,
                         "total_anomalies":anom,"avg_soh_pct":round(avg_soh,2)}),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin":"*"}
    )

async def run_http():
    app = web.Application()
    app.router.add_get("/diagnose", http_diagnose)
    app.router.add_get("/status",   http_status)
    app.router.add_get("/health",   lambda r: web.Response(text='{"ok":true}',
                                    content_type="application/json"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    print(f"[HTTP] API on http://localhost:{HTTP_PORT}")
    while True: await asyncio.sleep(3600)

async def async_main():
    await asyncio.gather(run_ws(), run_http())

def start_async():
    asyncio.run(async_main())

def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "xpc_backend_v4")
    client.on_connect = on_connect
    client.on_message = on_message
    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_forever()
    except Exception as e:
        print(f"[MQTT ERR] {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  EV Guardian XPC Backend v4.0 — FINAL (Physics-BMS Engine)")
    print("  Triple ONNX: Anomaly + SOH + PINN Battery Twin")
    print("=" * 70)

    init_db()

    global _anom_sess, _anom_inp, _soh_sess, _soh_inp, _pinn_sess, _pinn_inp, _fault_classifier
    _anom_sess, _anom_inp = load_session(ANOMALY_MODEL, "ANOMALY")
    _soh_sess,  _soh_inp  = load_session(SOH_MODEL,     "SOH    ")
    _pinn_sess, _pinn_inp = load_session(PINN_MODEL,    "PINN   ")

    print("[INIT] Loading FaultClassificationEngine...")
    try:
        from fault_classifier.inference import FaultClassificationEngine
        _fault_classifier = FaultClassificationEngine()
        print("[INIT] FaultClassificationEngine loaded successfully!")
    except Exception as e:
        print(f"[INIT CLASSIFIER ERR] {e}")

    t = threading.Thread(target=start_async, daemon=True)
    t.start()
    time.sleep(1.2)

    print(f"\n[READY] Dashboard  : open dashboard/index.html")
    print(f"[READY] WebSocket  : ws://localhost:{WS_PORT}")
    print(f"[READY] Diag API   : http://localhost:{HTTP_PORT}/diagnose?reason=CELL_3_VOLT_LOW")
    print(f"[READY] Status API : http://localhost:{HTTP_PORT}/status\n")

    try:
        start_mqtt()
    except KeyboardInterrupt:
        print(f"\n[STOP] Pkts={_packet_count}  Anomalies={_anomaly_count}")

if __name__ == "__main__":
    main()
