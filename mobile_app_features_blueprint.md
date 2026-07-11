# 📱 EV Guardian Companion App — OnePlus 15 Design & Feature Blueprint

This blueprint outlines the **exact screens, features, styling tokens, and interaction models** to implement in the Flutter/Android mobile companion app. It translates the flow coming from the **Arduino Uno Q** (Sensors + Sensor Trust Engine) and the **Snapdragon X PC** (Inference, eye-gaze, local LLM/VLM) into a premium, user-facing experience.

---

## 🎨 Creative Design & Aesthetics System

To make the app look extremely premium, use a curated dark-mode color scheme with glassmorphic panels and glowing neon status indicators.

### Color Palette (HSL & Hex)
* **Background Deep:** `#02040A` (Deepest Charcoal/Navy)
* **Neon Green (Healthy):** `hsl(140, 100%, 50%)` / `#00FF66`
* **Neon Amber (Warning):** `hsl(38, 100%, 50%)` / `#FF9900`
* **Neon Crimson (Alert):** `hsl(350, 100%, 50%)` / `#FF0055`
* **Tech Cyan (Active):** `hsl(185, 100%, 50%)` / `#00F2FE`
* **Glass Panel:** `rgba(15, 23, 42, 0.65)` with a backdrop filter blur of `16px` and a thin border of `rgba(255, 255, 255, 0.08)`.

---

## 🚨 The 5 Core Mobile Features to Build

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                        companion mobile app                             │
 ├───────────────────┬───────────────────┬─────────────────────────────────┤
 │ 📺 1. AR Lens     │ 🔊 2. Haptic Lens │ 📊 3. 3D Twin                   │
 │   Qualcomm SAM 2  │   Drowsiness/TTS  │   Cell status & voltage         │
 ├───────────────────┴───────────────────┴─────────────────────────────────┤
 │ 📉 4. Regen Budget Gauge (braking limit dial)                           │
 │ 💬 5. Conversational Co-pilot (driver local speech assistant)           │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

### FEATURE 1: AR Smart Diagnostics Lens (SAM 2 Powered)
* **Why it's useful:** The user points the OnePlus 15 camera at the physical battery pack. It instantly highlights which physical cell has a fault, avoiding cryptic text warnings.
* **How it works with the flow:** 
  1. Arduino reads low voltage on Cell 3 ➔ XPC runs model indicating a fault.
  2. Phone receives this over Bluetooth: `{"is_anomaly": true, "voltages": [3.8, 3.8, 2.1, 3.8]}`.
  3. Camera feed starts ➔ local quantized **SAM 2** (from Qualcomm AI Hub) segments the cells.
  4. Cell 3 segment is dynamically painted with a pulsing **Translucent Neon Crimson Cover** ($40\%$ opacity). Healthy cells (1, 2, and 4) are painted in **Soft Emerald Green**.
  5. A glassmorphic card floats in AR space pointing to Cell 3 showing: `"Cell 3: 2.10V (Alert - Loose Connection Tap detected)"`.

---

### FEATURE 2: Tactile Drowsiness Restorer & Audio Escape Driver
* **Why it's useful:** If the driver is falling asleep or distracted while a battery emergency happens, the phone acts as a physical wake-up co-pilot.
* **How it works with the flow:**
  1. XPC's camera detects the driver's gaze looking away or eyes closed (`attention_status: "DROWSY"`). Fuses this with Arduino telemetry.
  2. XPC relays a critical packet: `{"type": "alert", "driver_monitoring": {"status": "DROWSY"}}`.
  3. The phone activates OnePlus **O-Haptics** (high-definition linear vibration motor) in a sharp, pulsing pattern simulating road rumble strips.
  4. Plays a voice prompt (offline Text-to-Speech) using high-volume commands:
     *"Warning. Gaze loss detected while Battery Pack is hot. Wake up and pull over immediately."*
* **UI Design:** The entire phone screen flashes neon crimson, displaying giant, high-contrast text: **[ WAKE UP & PULL OVER ]**.

---

### FEATURE 3: Battery "Digital Twin" & Balancing Dial
* **Why it's useful:** Gives the user a visual twin representation of their battery pack health, cell variances, and STM32 sensor trust rating while charging or driving.
* **How it works with the flow:**
  1. Arduino Sensor Trust Engine calculates dynamic autoencoder error ➔ relays `{"trust_score": 98.2}` to XPC.
  2. XPC bundles this with individual cell states and relays to the phone.
  3. Phone displays a **glowing circular dashboard gauge** of the battery pack. 
  4. Tapping a cell zooms the UI into a detailed chart showing its real-time balance statistics (voltage vs current).
* **UI Design:** 
  * Background: Slate grey glassmorphism.
  * Central dial: Neon Cyan indicating **Sensor Trust Score (98% - High)**.
  * 4 glowing horizontal bars depicting voltages: if a cell drifts, its bar turns amber.

---

### FEATURE 4: Predictive Regen Current Limit Gauge
* **Why it's useful:** Regenerative braking inputs high charging currents. If cells are hot (from Arduino) or degraded, regen ruins them faster. This acts as a preventative co-pilot.
* **How it works with the flow:**
  1. Arduino measures battery charge acceptance capacity (SOH) and temperature.
  2. XPC's forward camera captures upcoming stop signs or vehicle deceleration.
  3. XPC calculates that a braking event is coming and the battery cannot accept high current.
  4. Phone updates a dynamic dial: it turns **Neon Amber** and limits the gauge to $20\%$.
  5. The phone gives tactile haptic clicks to the driver as they coast, recommending: *"Battery is warm. Use manual brakes gently to protect cell integrity."*

---

### FEATURE 5: Offline Voice Explainable AI Advisor
* **Why it's useful:** The driver can talk commands to the dashboard while keeping their hands on the wheel, asking for real-time risk explanations.
* **How it works with the flow:**
  1. Driver holds a button on their steering wheel or phone screen and talks: *"Is it safe to drive to the next charging station?"*
  2. Phone records the audio, passes it to the XPC (runs Whisper).
  3. XPC feeds the text along with Arduino telemetry and the driver's attentiveness into **Qwen2.5-3B**.
  4. LLM response is returned to the phone over Bluetooth:
     *"Yes, Cell 3 voltage drop is diagnosed as a loose balancing wire (vibration correlated). The cell is physically healthy. You can safely drive 12 miles to the next station."*
  5. Phone speaks the advice back to the driver clearly.
* **UI Design:** A smooth, pulsing neon cyan visualizer orb at the center of the screen that expands and contracts dynamically in response to speaker frequencies.
