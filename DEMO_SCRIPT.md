# EV Guardian — Hackathon Demo Script
## Qualcomm Snapdragon Multiverse Hackathon 2026

---

## Project Elevator Pitch (30 seconds)

> *"EV Guardian is a real-time, AI-powered battery health monitoring system that runs entirely on the edge — no cloud required. It uses the Qualcomm Snapdragon X Copilot+ PC's Hexagon NPU to run two ONNX models simultaneously: an Isolation Forest for anomaly detection and a Gradient Boosting regressor for State-of-Health estimation. The Arduino UNO Q captures sensor data, a gateway daemon bridges it to MQTT, and a 3D digital twin dashboard displays live battery health — with an LLM diagnostic copilot for fault analysis. Long-term fleet analytics flow to Qualcomm Cloud AI 100."*

---

## Demo Flow (5 minutes)

### Step 1 — Show the Architecture (30s)
Open `EV_GUARDIAN_FLOWCHART.html` in browser.

**Say:** *"Here's our full hardware-to-cloud pipeline. Data flows from the STM32U585 sensor MCU → QRB2210 gateway → Snapdragon X backend → dashboard and Cloud AI 100."*

---

### Step 2 — Start the System (1 min)

Open **3 terminals**:

**Terminal 1 — Backend:**
```bash
cd "C:\ev vechile"
python backend.py
```
Expected output:
```
[ONNX] ANOMALY  | anomaly_model.onnx   providers=['CPUExecutionProvider']
[ONNX] SOH      | models/soh_model.onnx providers=['CPUExecutionProvider']
[DB]   Ready: 'ev_telemetry.db'
[WS]   Server on ws://localhost:8765
[HTTP] API on http://localhost:8766
[MQTT] Connected. Subscribed to 'ev/sensor/telemetry'
```

**Terminal 2 — Sensor Simulator:**
```bash
python dummy_publisher.py
```
**Terminal 3 — Cloud Sync:**
```bash
python cloud_sync.py --mock-server
```

**Say:** *"The backend loads both ONNX models, opens a WebSocket server, and starts listening for MQTT telemetry at 10Hz."*

---

### Step 3 — Open the Dashboard (30s)

Open `dashboard/index.html` in Chrome/Edge.

**Point out:**
- ⚡ **Top row KPIs**: Packet counter incrementing at 10Hz, anomaly counter, live SOH%
- 🔋 **3D Battery Pack**: 4 cells rendering in real-time with fill-level and voltage
- 📊 **4 Sparkline Charts**: Voltages, temperatures, current, SOH trend
- 🤖 **Dual AI Gauges**: Anomaly Risk % + State of Health % side by side

**Say:** *"Every packet is vectorized into a 1×8 float32 array and passed through two ONNX models on the Hexagon NPU. The results update the dashboard in under 10ms."*

---

### Step 4 — Trigger a Live Fault (1 min)

Wait for the **FAULT STATE** cycle (every 10 seconds the dummy publisher injects):
- Cell 3 voltage: **1.25V** (wire disconnect simulation)
- Cell 3 temperature: **115.2°C** (thermal fault)

**What the audience sees:**
- 🔴 Red alert banner: `ANOMALY DETECTED — ev-uno-q-01`
- 🔴 Cell 3 turns red with pulsing `!` indicator
- 📉 SOH gauge drops
- Console prints `*** ANOMALY #N ***` with `CELL_3_VOLT_LOW|CELL_3_TEMP_HIGH`

**Say:** *"The Isolation Forest model detects the anomaly from the raw feature vector in real-time. The hard-threshold rules catch the voltage drop as a wire disconnect fault."*

---

### Step 5 — LLM Diagnostic Copilot (45s)

Click the **"🤖 AI Diagnosis"** button in the dashboard.

**Expected response panel shows:**
```
ROOT CAUSE: A cell voltage below 2.5V is a strong indicator of a
wire harness disconnect or BMS measurement channel failure...

IMMEDIATE ACTIONS:
  • Isolate the vehicle: disable HV contactor
  • Check connector pins J3/J4 on BMS board
  • Do NOT charge until root cause confirmed

RECOMMENDED FIX: If connector is intact, perform capacity test...
```

**Say:** *"The diagnostic copilot uses our local LLM engine — it checks for Ollama first, then falls back to our domain-specific rule engine. No internet required."*

---

### Step 6 — Cloud AI 100 Analytics (30s)

Show Terminal 3:
```
[CLOUD] Batch #1 received: 50 records (12 anomalies) Total ingested: 50
[CLOUD] Batch #2 received: 50 records (8 anomalies)  Total ingested: 100
```

Or run:
```bash
python cloud_sync.py --report
```
Output:
```
  Total Packets    : 2,400+
  Total Anomalies  : 240+
  Anomaly Rate     : ~10%
  Avg Cell Voltages: [3.82, 3.81, x.xx, 3.80] V
```

**Say:** *"Anomaly summaries are batched and uploaded to our Qualcomm Cloud AI 100 endpoint every 60 seconds for fleet-wide analytics and model retraining."*

---

### Step 7 — Show the Firmware (30s)

Open `firmware/main.c` — scroll past Thread A and Thread B.

**Say:** *"On the actual Arduino UNO Q, our Zephyr RTOS firmware runs two threads: Thread A does 10Hz sensor acquisition with 5-tap moving-average filtering and writes to IPC shared SRAM. Thread B reads the trust status flag from the Snapdragon X and updates the LED matrix alarm."*

---

## Key Technical Differentiators

| Feature | Implementation |
|---------|---------------|
| **Edge AI** | Dual ONNX models (Anomaly + SOH) on Hexagon NPU via QNN EP |
| **Deterministic Sensing** | Zephyr RTOS Thread A: 100ms guaranteed period, 5-tap MA filter |
| **Offline Resilience** | Store-and-forward cache in gateway, BLE emergency beacon |
| **LLM Copilot** | Ollama Llama-3 local inference + expert rule fallback (no cloud) |
| **Full-Stack Edge** | Firmware → Gateway → AI Backend → Dashboard → Cloud — all connected |

---

## Model Specifications

| Model | Algorithm | Train Samples | Accuracy | Size |
|-------|-----------|--------------|----------|------|
| `anomaly_model.onnx` | Isolation Forest | 5,000 healthy | 95% normal rate | 1.1 MB |
| `models/soh_model.onnx` | Gradient Boosting Regressor | 8,000 aging profiles | MAE = 2.37% | 199 KB |

**Input to both models:** `[c1_v, c2_v, c3_v, c4_v, current_a, max_temp_c, vibration_g, gas_ppm]`

---

## Q&A Preparation

**Q: Why ONNX Runtime instead of TFLite?**
> ONNX Runtime with QNN EP natively targets the Qualcomm Hexagon NPU (HTP), enabling INT8 acceleration on Snapdragon X without model conversion. TFLite would require additional delegates.

**Q: How does the system handle connectivity loss?**
> The QRB2210 gateway daemon writes telemetry to a local JSONL cache file when MQTT is unreachable, and spools it on reconnect. BLE broadcasts safety state regardless.

**Q: What's the inference latency?**
> On CPU: ~1–2ms per model. On Hexagon NPU via QNN EP: sub-1ms (INT8 quantized). Both models run per-packet at 10Hz.

**Q: How is SOH calculated?**
> Our Gradient Boosting model correlates cell voltage imbalance (degraded cells drift lower), internal resistance proxy (temperature rise at same current), and gas ppm (electrolyte decomposition) to produce a 0–100% health estimate.

**Q: What sensors does the hardware use?**
> Cell voltages (resistor divider + ADC), current (ACS712-05B Hall effect), temperature (NTC thermistor), vibration (MPU6050 I2C MEMS IMU), and gas (MQ-2 semiconductor).

---

## Quick Reference — Port Map

| Service | Address |
|---------|---------|
| MQTT Broker | `localhost:1883` |
| WebSocket Server | `ws://localhost:8765` |
| HTTP Diagnostics API | `http://localhost:8766/diagnose` |
| Status API | `http://localhost:8766/status` |
| Mock Cloud AI 100 | `http://localhost:9000` |
| Ollama LLM (optional) | `http://localhost:11434` |

---

## One-Line Start

```bash
python launch.py
```
