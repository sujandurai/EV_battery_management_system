# EV Guardian AI: Complete Node-to-Node Functional & Data Flow Specification

This guide details the complete data flows, packet formats, API bindings, and fallback mechanics for the four primary components of the **EV Guardian AI** architecture: the **Arduino UNO Q (MCU + MPU)**, **Snapdragon X Copilot+ PC (XPC)**, **Companion Mobile Phone**, and the **Qualcomm Cloud AI 100 Fleet Cloud**.

---

## 1. Complete System Flowchart & Interaction Topology

The following flowchart illustrates the physical and network connections, protocols, and data structures between the edge sensing node, the regional computing hub, the user dashboard, and the centralized AI fleet manager:

```mermaid
graph TB
    %% Nodes Def
    subgraph UNO_Q ["Arduino UNO Q Gateway Node"]
        direction TB
        subgraph STM32_Core ["STM32U585 Core (Zephyr RTOS)"]
            AcqThread["Thread 1: Sensor Loop (100ms)"]
            DSP["TinyML/DSP Noise Filter"]
            DispThread["Thread 3: Status Matrix (500ms)"]
        end
        subgraph Q_Bridge ["RPC Inter-Core Bridge"]
            SRAM[("Shared RAM Table")]
        end
        subgraph MPU_Core ["QRB2210 Linux Core (Debian)"]
            GatewayPy["gateway.py (100ms poll)"]
            Storage["eMMC Caching Spooler"]
            GATT["BLE GATT Server"]
        end
    end

    subgraph XPC_Hub ["Snapdragon X Copilot+ PC (Local Vehicle Host)"]
        Broker["Mosquitto MQTT Broker"]
        BackendPy["backend.py (Ingestion Engine)"]
        subgraph Hexagon_NPU ["Hexagon NPU (QNN EP)"]
            TrustModel["Sensor Trust Classifier"]
            SOHModel["SOH Estimation Model"]
        end
        WSServer["WebSocket Server (port 8080)"]
        DashApp["React / Three.js 3D Digital Twin UI"]
        CopilotCache[("Local Context Prompt Buffer")]
        OllamaLLM["Llama-3-8B-Instruct (Local LLM)"]
    end

    subgraph Mobile_App ["Companion Mobile Application"]
        UI_Home["Home Dashboard (SoC% / SoC Dial)"]
        UI_Alerts["Live push Alerts (Red Anomaly Alert)"]
        BLE_Tool["BLE Diagnostic Fallback Viewer"]
    end

    subgraph Cloud_AI100 ["Qualcomm Cloud AI 100 Fleet Center"]
        Ingress["Fleet Telemetry Ingress"]
        DB[("Fleet Historical DB")]
        Retrain["Model Retraining Pipeline"]
        AIHubCompiler["Qualcomm AI Hub Model Export"]
    end

    %% Flow lines
    %% 1. Sensing to Bridge
    AcqThread -->|Raw analog/I2C measurements| DSP
    DSP -->|Noise-reduced data float arrays| SRAM
    SRAM -->|Read variables via Bridge.get()| DisplayData[LED Matrix Display]
    DispThread -->|Read trust_status flag| SRAM
    
    %% MPU to Bridge & External
    SRAM <-->|Shared Bus Exchange| GatewayPy
    GatewayPy -->|Write trust_status updates| SRAM
    GatewayPy -->|If Network Offline: Backup logs| Storage
    GatewayPy -->|Expose local BLE service| GATT
    
    %% Network bridge
    GatewayPy -->|Wi-Fi 5 / JSON over MQTT| Broker
    Broker -->|MQTT telemetry payload stream| BackendPy
    
    %% PC Processing
    BackendPy -->|Prepare 1x8 telemetry vector| Hexagon_NPU
    Hexagon_NPU -->|NPU inferences: Trust Score % & SOH %| WSServer
    WSServer -->|Local WebSocket Live Feed| DashApp
    BackendPy -->|Incident triggers: MQTT| Broker
    Broker -->|ev/analytics/trust_status updates| Broker
    Broker -->|Feedback status stream| GatewayPy
    
    %% Copilot
    BackendPy -->|Logs to circular cache| CopilotCache
    DashApp <-->|Diagnostic natural language query| OllamaLLM
    CopilotCache -->|Context Injection| OllamaLLM

    %% Mobile Connections
    WSServer -->|Local Network WebSockets| UI_Home
    WSServer -->|Status Flags | UI_Alerts
    GATT <-->|Bluetooth 5.1 Local Fallback link| BLE_Tool

    %% Cloud Operations
    BackendPy -->|HTTPS Post: Daily Summary payload| Ingress
    Ingress --> DB
    DB --> Retrain
    Retrain --> AIHubCompiler
    AIHubCompiler -->|OTA updates: Compiles newer QNN weights| Hexagon_NPU
```

---

## 2. In-Depth Operational Node Analysis

### 2.1 Arduino UNO Q (Edge Sensing Node)
The Arduino UNO Q houses dual processing architectures on a single gateway footprint, implementing real-time data acquisition alongside Linux networking stacks.

```
[ PHYSICAL SENSORS ]
  ├── Cell 1-4 Voltage (Analog Divider pins A0-A3)
  ├── Current ACS712   (Analog pin A4)
  ├── Temperature      (Analog pin A5)
  ├── Gas Sensor MQ-2  (Analog pin A6)
  └── Vibration Sensor (I2C Regis. MPU6050)
            │
            ▼ (12-bit SAR ADC & 400kHz I2C Bus)
[ STM32U585 MCU Core (Zephyr OS) ]
  ├── Thread 1: Sensor Loop (100ms Task)
  │     reads hardware registers & converts measurements
  ├── TinyML/DSP Filter
  │     5-tap median/moving average noise reduction
  └── Thread 2: Bridge Writer (100ms Task)
        formats floats to CSV string & updates variables
            │
            ▼ (Qualcomm Hardware IPC Bus)
[ INTER-CORE BUS BRIDGE ]
  ├── Shared Memory space mapping parameters:
  │     "voltages", "temperature", "current", "vibration", "gas", "trust_status"
            │
            ▼ (Bridge.get() / Bridge.put() memory exchange)
[ QRB2210 Linux Gateway MPU Core (Debian) ]
  ├── gateway.py (100ms background thread)
  │     packages floats into structured JSON payload
  ├── eMMC Store-and-Forward Cache Handler
  │     logs telemetry to root storage if connection lost
  └── BLE GATT Server
        advertises status packets locally over Bluetooth
```

#### Detailed Operations & Functional Sequence:
1. **STM32 Core Boot & Thread Registration**:
   * Zephyr RTOS boots.
   * Registers a hardware task scheduler executing three concurrent threads:
     * **Thread 1 (Sensing Task)**: Wakes up every 100ms (Priority 5, Preemptible).
     * **Thread 2 (RPC Bridge Writer Task)**: Executed inline after Thread 1 finishes scaling and filtering values. Writes telemetry strings to parameters in shared memory.
     * **Thread 3 (Display Update Task)**: Wakes up every 500ms (Priority 10, Low-priority).
2. **Sensing Loop & DSP Preprocessing**:
   * Thread 1 samples cell voltages, temperatures, current, and gas through ADC register commands.
   * Requests three-axis accelerations from the MPU6050 accelerometer over I2C.
   * Runs local DSP code (e.g. 5-tap moving median or low-pass digital filters) to smooth high-frequency EMI noise from the EV drivetrain.
3. **IPC Inter-Core Transmission**:
   * Thread 2 calls `Bridge.put()` with string-encoded values to synchronize them with the dual-port SRAM mapping registers monitored by the QRB2210 Linux subsystem.
4. **Gateway Processing (gateway.py)**:
   * A Python daemon runs on Debian at 100ms intervals, checking values from storage using `bridge.get()`.
5. **Connection Fallback & Local Advertising**:
   * **If Network Status = Connected**: Serializes the elements to JSON and publishes the telemetry payload to the Snapdragon X PC via Wi-Fi.
   * **If Network Status = Disconnected**: Swings data records directly to an onboard SQLite or flat JSON cache file on the eMMC container. Once connection resumes, a dedicated spooler thread flushes accumulated records to the broker.
   * Exposes raw parameters via a Bluetooth 5.1 GATT Server, allowing local diagnostic access if the master Wi-Fi network is down.

---

### 2.2 Snapdragon X Copilot+ PC (Edge AI Hub)
The Snapdragon PC handles local AI inference, WebSocket data broadcasting, Three.js digital twin visualization, and offline natural language diagnostic copilots.

```
MQTT Telemetry Feed (ev/sensor/telemetry)
  │
  ▼
[ python Ingestion Engine (backend.py) ]
  ├── parses MQTT input JSON
  ├── stores statistics to local database
  └── vectorizes values to numpy array [C1, C2, C3, C4, Current, Temp, Vib, Gas]
        │
        ▼ (Shape [1, 8] Float32)
[ Hexagon NPU Inference Engine (QNN EP) ]
  ├── Sensor Trust Classifier (ONNX) ──► trust classification + Trust %
  └── State of Health Regressor (ONNX) ──► estimated SOH %
        │
        ▼ (predictions output payload)
[ Local WebSocket Server (port 8080) ] & [ local Cache Buffer ]
  ├── Broadcasts live metrics frames
  │     ├── React/Three.js 3D Digital Twin Visualizer
  │     └── Companion Phone app (Live telemetry charts)
  │
  └── Rolling buffer injection (Last 1,000 readings)
        │
        ▼ (RAG Context Injection Prompt builder)
[ Ollama Llama-3-8B-Instruct (NPU/GPU accelerated) ]
  └── Ingests diagnostic prompts & responds to chat queries
```

#### Detailed Operations & Functional Sequence:
1. **MQTT Telemetry Ingress**:
   * Ingests JSON messages containing telemetry from the gateway on the topic `ev/sensor/telemetry` via the local Mosquitto MQTT broker.
2. **Feature Serialization**:
   * The Python daemon extracts values from the JSON and builds an 8-feature representation vector: `[Cell1_V, Cell2_V, Cell3_V, Cell4_V, Current_A, Temp_C, Vibration_G, Gas_PPM]`.
3. **Hexagon NPU Model Evaluation**:
   * Offloads execution to the Hexagon NPU using ONNX Runtime with the `QnnExecutionProvider`.
   * **Sensor Trust Model**: Classifies sensor state, predicting individual cell trust scores (e.g. identifying a loose voltage tap on Cell 3).
   * **State of Health Model**: Estimates overall capacity retention.
4. **UI Feedback Loop**:
   * Broadcasts telemetry, trust predictions, and SOH metrics to a WebSocket server (`ws://localhost:8080`).
   * Updates Three.js card components (e.g., highlighting anomalous cells in red) and updates the companion phone dashboard.
   * If a critical sensor trust failure is detected:
     * Publishes a feedback warning payload (`FAULT`) to the MQTT broker on the topic `ev/analytics/trust_status`.
     * The QRB2210 receives this warning and writes it back to the STM32 via `Bridge.put("trust_status", "FAULT")`, triggering the LED Matrix alert sequence.
5. **Local Chat Copilot & Context Caching**:
   * Caches a rolling history of the last 1,000 metrics packets.
   * When queried (e.g., *"Why is Cell 3 warning?"*), the system injects the compiled rolling log history into a local Llama-3-8B model, outputting human-readable diagnostics offline.

---

### 2.3 Companion Mobile Application (Driver Remote Monitor)
The companion application acts as the user's primary interface for status monitoring, push notifications, and local offline diagnostics.

```
                  ┌──────────────────────────────────────────┐
                  │                 USER'S PHONÈ             │
                  └───────┬──────────────────────────┬───────┘
                          │                          │
                 (Local WebSockets)          (Local Bluetooth 5.1)
                          │                          │
                          ▼                          ▼
            ┌───────────────────────────┐      ┌───────────────────────────┐
            │   NORMAL OPERATION VIEW   │      │    BLE DIAGNOSTIC VIEW    │
            ├───────────────────────────┤      ├───────────────────────────┤
            │ • Circular SoC% Dial Gauge│      │ • Immediate Local Connect │
            │ • Temperature Heatmap     │      │ • Read raw cell states    │
            │ • Core SOH Estimation %   │      │ • Bypass missing Wi-Fi    │
            │ • Read local fault lists  │      │ • Read local fault lists  │
            └─────────────┬─────────────┘      └───────────────────────────┘
                          │
                          ▼
            ┌─────────────┴─────────────┐
            │    PUSH ALARMS ENGINE     │
            ├───────────────────────────┤
            │ • Listener for Anomaly JSON│
            │ • Sound warning on FAULT  │
            │ • Show cell warning type  │
            └───────────────────────────┘
```

#### Detailed Operations & Functional Sequence:
1. **Normal Operational Ingress**:
   * Listens to the local WebSocket server to receive updates.
   * Renders real-time metrics on a dashboard, updating a circular State of Charge (SoC%) dial gauge and cell temperature grids.
2. **Push Notification Alarms**:
   * If an anomaly is identified (e.g. a wire disconnect code `SENSOR_DISCONNECT` with low trust score), the app displays a full-screen safety warning, vibrating and sounding an alert to warn the operator.
3. **Local BLE Fallback Mode**:
   * If cellular and Wi-Fi networks are unavailable:
     * The mobile app scans for local BLE advertisements from the Arduino UNO Q.
     * Establishes a direct GATT connection to the QRB2210 Linux core's BLE client interface.
     * Polls raw voltage, current, and temperature variables directly from the hardware, ensuring diagnostic capabilities remain functional.

---

### 2.4 Qualcomm Cloud AI 100 (Fleet Analytics Platform)
The Cloud AI 100 platform manages cloud analytics, model optimization, and fleet-wide retraining.

```
       Daily Summaries        Incident Reports & Snapshots
              │                             │
              └──────────────┬──────────────┘
                             │
                             ▼ (HTTPS Rest Ingress)
              ┌──────────────┴──────────────┐
              │ Qualcomm Cloud Database     │
              │ Fleet Logs Aggregation      │
              └──────────────┬──────────────┘
                             │
                             ▼
              ┌──────────────┴──────────────┐
              │ Fleet Model Retraining      │
              │ Runs INT8 quantization      │
              └──────────────┬──────────────┘
                             │
                             ▼ (ONNX files compile)
              ┌──────────────┴──────────────┐
              │ Qualcomm AI Hub Compiler    │
              │ target: Snapdragon X Elite  │
              └──────────────┬──────────────┘
                             │
                             ▼ (Network OTA Broadcast)
               Snapdragon PC Client Nodes
```

#### Detailed Operations & Functional Sequence:
1. **Fleet Logging & Daily Summaries**:
   * Every 24 hours, active vehicle hosts upload condensed operational summaries (VIN, mileage, average SOH, cycle counts, and peak thermal events) to the cloud environment.
2. **Incident Snapshot Processing**:
   * When the edge PC NPU flags a critical safety event:
     * It captures a diagnostic snapshot containing the surrounding 10 seconds of sensor telemetry.
     * Uploads the snapshot to the Cloud AI 100 incident API for technical investigations and fleet-wide tracking.
3. **Model Optimization Pipeline**:
   * The training engine processes fleet logs to optimize safety models.
   * Leverages the **Qualcomm AI Hub** compiler toolset to convert and optimize compiled models:
     ```bash
     qai-hub-client.compile(
         model="sensor_trust.onnx",
         device="Snapdragon X Elite Compute",
         input_specs={"telemetry_vector": [1, 8]},
         options="--target_runtime qnn"
     )
     ```
   * Quantizes weights to INT8 to enable low-latency inference on Hexagon NPUs.
4. **OTA Model Distribution**:
   * Distributes optimized models back to the Snapdragon PCs, updating local diagnostic capabilities.

---

## 3. Data Structures & Contract Protocols

### 3.1 Arduino MPU-to-Host PC: Telemetry Schema (Topic: `ev/sensor/telemetry`, Protocol: MQTT JSON)
Sent every 100ms from the QRB2210 MPU over Wi-Fi.

```json
{
  "timestamp": 177904008,
  "device_id": "ev-uno-q-01",
  "cells": {
    "voltage_v": [3.82, 3.81, 1.25, 3.83],
    "temp_c": [34.2, 34.2, 34.2, 34.2]
  },
  "pack": {
    "current_a": -12.4,
    "vibration_g": 0.08,
    "gas_ppm": 12
  },
  "metadata": {
    "node_status": "OK"
  }
}
```

### 3.2 Host PC-to-Arduino MPU: Feedback Schema (Topic: `ev/analytics/trust_status`, Protocol: MQTT Text)
Sent by the edge PC when an anomaly is identified.

```json
"FAULT"
```

### 3.3 Host PC-to-Cloud System: Fleet Analytics Schema (Protocol: HTTPS POST JSON)
Uploaded every 24 hours or upon diagnostic event request.

```json
{
  "vehicle_vin": "EVG-8947A-SNAP",
  "total_mileage_km": 14205.8,
  "battery_pack_summary": {
    "total_charge_cycles": 342,
    "average_soh_percent": 94.2,
    "degradation_rate_per_100cycles": 0.35,
    "peak_operating_temp_c": 115.2
  },
  "cell_specific_averages": {
    "avg_voltages_v": [3.82, 3.81, 1.25, 3.83],
    "avg_temps_c": [34.2, 34.2, 115.2, 34.2],
    "voltage_delta_max_v": 2.58
  },
  "incident_log": {
    "anomaly_flagged": "SENSOR_DISCONNECT",
    "calculated_trust_pct": 14.5
  }
}
```

---

## 4. End-to-End System Interface Contract

The table below outlines the timing, protocol, and data requirements for each interface across the system:

| Interface | From Node | To Node | Protocol / Speed | Data Format | Critical Constraints |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Sensing Bus** | Physical Sensors | STM32U585 MCU | Analog (ADC) / I2C (400kHz) | Raw Volts & Accelerometer registers | Extended sample time configuration |
| **Inter-Core Bridge** | STM32U585 Core | QRB2210 Gateway | Qualcomm IPC / Shared RAM | Character arrays (CSV format) | Updated at 100ms intervals |
| **Edge Pipeline** | QRB2210 MPU | Snapdragon X PC | Wi-Fi 5 / MQTT | JSON payload structure | Fallback to eMMC on loss of Wi-Fi |
| **Feedback Path** | Snapdragon X PC | QRB2210 MPU | Wi-Fi 5 / MQTT | Raw status text | Triggered when anomaly is identified |
| **Local Broadcast** | Snapdragon X PC | Dashboard / Mobile | WebSockets (port 8080) | Analytics JSON payload | Real-time update rate |
| **Local Assistant** | Backend Py Cache | Local LLM | Ollama API / local | diagnostics injection prompt | Offline execution on Edge PC |
| **Direct Fallback** | QRB2210 Gateway | Companion Phone | Bluetooth Low Energy | GATT Client descriptors | Fallback when Wi-Fi is unavailable |
| **Fleet Analytics** | Snapdragon X PC | Cloud AI 100 | HTTPS Post Endpoint | Compressed summary JSON | Daily upload schedule |
| **OTA Pipeline** | Cloud AI Hub | Snapdragon X PC | System Network | Compiled ONNX/QNN files | Updates local NPU inferences |
