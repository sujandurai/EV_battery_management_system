# EV Guardian — Quick Start Guide

## Prerequisites
Make sure these are installed and running:
```
pip install paho-mqtt onnxruntime scikit-learn skl2onnx websockets aiohttp requests numpy
```
Also install and start **Mosquitto MQTT Broker** (port 1883):
- Download: https://mosquitto.org/download/
- Start: `net start mosquitto` (Windows)

---

## One-Click Start
```bash
python launch.py
```
This opens **4 console windows** and your browser automatically.

---

## Manual Step-by-Step

### Terminal 1 — Train ONNX Model (one-time only)
```bash
python train_anomaly_model.py
```

### Terminal 2 — Mock Cloud Server + Sync
```bash
python cloud_sync.py --mock-server
```

### Terminal 3 — XPC Backend (core engine)
```bash
python backend.py
```

### Terminal 4 — Arduino Simulator
```bash
python dummy_publisher.py
```

### Browser — Dashboard
Open `dashboard/index.html` directly in Chrome/Edge.

---

## Architecture
```
[Arduino UNO Q Simulator]   →  MQTT (port 1883)
  dummy_publisher.py              ↓
                            backend.py (XPC Backend v3)
                            ├── ONNX IsolationForest inference
                            ├── SQLite persistence (ev_telemetry.db)
                            ├── WebSocket broadcast (port 8765)
                            └── HTTP Diagnostics API (port 8766)
                                   ↓                ↓
                            dashboard/         llm_diagnostics.py
                            index.html         (Ollama + rule engine)
                                   ↓
                            cloud_sync.py → Cloud AI 100 (port 9000 mock)
```

## File Overview
| File | Role |
|------|------|
| `dummy_publisher.py` | Simulated Arduino UNO Q sensor (10 Hz MQTT) |
| `backend.py` | Core engine: MQTT + ONNX + WebSocket + HTTP API |
| `train_anomaly_model.py` | Train & export IsolationForest to ONNX |
| `anomaly_model.onnx` | Trained AI model (1×8 float32 → anomaly label) |
| `llm_diagnostics.py` | LLM copilot (Ollama + rule fallback) |
| `cloud_sync.py` | Fleet sync to Cloud AI 100 endpoint |
| `verify_db.py` | Debug: inspect SQLite database records |
| `launch.py` | One-click launcher for all services |
| `dashboard/index.html` | 3D Digital Twin web dashboard |
| `ev_telemetry.db` | SQLite telemetry + anomaly log |

## Fault Simulation
The dummy publisher automatically cycles:
- **0–10s**: HEALTHY STATE (normal voltages 3.78–3.84V, temp 34°C)
- **10–20s**: FAULT STATE (Cell 3 → 1.25V, Temp → 115°C)
- Repeats every 20 seconds
