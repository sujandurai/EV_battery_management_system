# EV Guardian — Complete System Architecture & Operations Dossier

This document provides the complete end-to-end technical specifications, mathematical derivations, wiring pinouts, communication protocols, and machine learning integration details of the **EV Guardian Edge AI Battery Management System**.

---

## 1. Hardware Architecture & Pin Assignments

The EV Guardian system utilizes a dual-processor architecture on the **Arduino Uno Q** board, splitting duties between a high-efficiency microcontroller (STM32U585) and a Linux-capable microprocessor (Qualcomm QRB2210).

### Physical Pin Mapper (STM32U585 MCU)

| Pin Name | Physical Sensor | Signal/Protocol Type | Electrical Specifications | Primary Function |
| :--- | :--- | :--- | :--- | :--- |
| **A0** | Cell 1 Tap | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ ($0 - 16383$ raw) | Measures Cell 1 Positive Terminal ($0\text{V} - 4.2\text{V}$ voltage range) |
| **A1** | Cell 2 Tap | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ ($0 - 16383$ raw) | Measures Cell 2 Positive Terminal ($4.2\text{V} - 8.4\text{V}$ cumulative voltage) |
| **A2** | Cell 3 Tap | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ ($0 - 16383$ raw) | Measures Cell 3 Positive Terminal ($8.4\text{V} - 12.6\text{V}$ cumulative voltage) |
| **A3** | Cell 4 Tap | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ ($0 - 16383$ raw) | Measures Cell 4 Positive Terminal ($12.6\text{V} - 16.8\text{V}$ cumulative voltage) |
| **A4** | ACS712-05B | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ (divided from $5\text{V}$) | Measures Current Sensor output (onboard $2:1$ hardware divider active) |
| **A5** | MQ-7 Gas | Analog Input (ADC) | $0\text{V} - 3.3\text{V}$ (divided from $5\text{V}$) | Measures Carbon Monoxide analog voltage ($3.2$ divider ratio active) |
| **D4** | DS18B20 #1 | Digital (1-Wire Bus) | $3.3\text{V}$ Logic (INPUT_PULLUP) | Core temperature probe for cell group 1 & 2 |
| **D5** | DS18B20 #2 | Digital (1-Wire Bus) | $3.3\text{V}$ Logic (INPUT_PULLUP) | Core temperature probe for cell group 3 & 4 |
| **SDA (D18)**| MPU-6050 | Digital (I2C) | $3.3\text{V}$ Logic (400kHz speed) | Fast Data Line for Accelerometer/Gyroscope values |
| **SCL (D19)**| MPU-6050 | Digital (I2C) | $3.3\text{V}$ Logic (400kHz speed) | Fast Clock Line for Accelerometer/Gyroscope values |

---

## 2. Firmware Telemetry Calculations & Calibration Derivations

All raw digital-to-analog values are calculated strictly inside the STM32's `loop()` routine.

### A. Voltage Stack Tap Subtraction Logic
To prevent short circuits, the stack shares a common ground. The ADC taps measure the cumulative voltage from ground upwards.
The raw voltage at each ADC port is computed as:
$$V_{\text{port}} = \text{RawADC} \times \left( \frac{3.3\text{V}}{16383} \right)$$
The cumulative voltages are restored by applying the physical resistor divider scaling factors:
$$V_{\text{tap1}} = V_{\text{port0}} \times \text{SCALE\_FACTOR\_C1}$$
$$V_{\text{tap2}} = V_{\text{port1}} \times \text{SCALE\_FACTOR\_C2}$$
$$V_{\text{tap3}} = V_{\text{port2}} \times \text{SCALE\_FACTOR\_C3}$$
$$V_{\text{tap4}} = V_{\text{port3}} \times \text{SCALE\_FACTOR\_C4}$$
Individual cell voltages ($V_{\text{cell}}$) are resolved by subtracting lower taps, applying calibration fine-tuning weights:
$$\text{Cell 1} = V_{\text{tap1}} \times \text{CALIB\_TAP1}$$
$$\text{Cell 2} = (V_{\text{tap2}} \times \text{CALIB\_TAP2}) - \text{Cell 1}$$
$$\text{Cell 3} = (V_{\text{tap3}} \times \text{CALIB\_TAP3}) - (V_{\text{tap2}} \times \text{CALIB\_TAP2})$$
$$\text{Cell 4} = (V_{\text{tap4}} \times \text{CALIB\_TAP4}) - (V_{\text{tap3}} \times \text{CALIB\_TAP3})$$

### B. Current Scaling & Offset Drift Compensation
The ACS712 Hall-Effect sensor outputs an analog voltage at $5\text{V}$. The Uno Q boards routes this to Pin A4 through a $2:1$ divider. The true sensor output ($V_{\text{sensor}}$) is:
$$V_{\text{sensor}} = \left(\text{RawADC}_{\text{curr}} \times \frac{3.3\text{V}}{16383}\right) \times 2.0$$
$$\text{Current (Amps)} = \frac{V_{\text{sensor}} - V_{\text{offset}}}{\text{Sensitivity}}$$
*   **$V_{\text{offset}}$** = $2.39\text{V}$ (zero-current point calibration).
*   **$\text{Sensitivity}$** = $0.185\text{V/A}$ (for 5A range models).
*   **Zero-drift clamping:** If $\left|V_{\text{sensor}} - V_{\text{offset}}\right| < 0.03\text{V}$ or $V_{\text{sensor}} < 0.2\text{V}$, the output is clamped to exactly $0.00\text{A}$ to eliminate transient magnetic noise.

### C. Live MPU-6050 Gravity Removal & Vibration Isolation
When static, the accelerometer reports gravity ($1.0g$). To calculate clean vibration without sensor orientation bias:
1. **Rest Calibration (at startup in `setup()`):**
   $$g_{\text{base}} = \frac{1}{SampleCount}\sum_{i=1}^{SampleCount} \sqrt{a_{x,i}^2 + a_{y,i}^2 + a_{z,i}^2}$$
   *(A sample count of 50 samples is taken over a 500ms startup delay).*
2. **Dynamic Vibration Magnitude Calculation:**
   $$Vib_{\text{mag}} (g) = \left| \sqrt{a_x^2 + a_y^2 + a_z^2} - g_{\text{base}} \right|$$
   $$Vib_{\text{metric}} (\text{m/s}^2) = Vib_{\text{mag}} \times 9.80665$$

### D. MQ-7 Gas Sensor ppm Conversion
The MQ-7 outputs are scaled up by $3.2$ corresponding to the onboard loading resistor network divider ratio.
1. **Sensor Resistance ($R_s$):**
   $$V_{\text{sensor}} = V_{\text{pin}} \times 3.2$$
   $$R_s = R_L \times \left( \frac{V_{cc} - V_{\text{sensor}}}{V_{\text{sensor}}} \right)$$
2. **Power-Law PPM Conversion:**
   $$\text{CO (ppm)} = A \times \left( \frac{R_s}{R_o} \right)^{B}$$
   *Where $R_L = 10\text{k}\Omega$, $R_o = 4.2\text{k}\Omega$, $A = 99.8$, and $B = -1.45$.*
   *If $V_{\text{sensor}} < 0.40\text{V}$, the sensor is categorized as disconnected/grounded, clamping CO output to $0.0\text{ppm}$.*

---

## 3. Inter-Processor Communication (IPC) Protocol

Data flows from the local STM32 controller to the host processor side over a high-speed serial UART bus interface:

1. **Physical Settings:** Baud Rate: **115200**, Data Bits: **8**, Stop Bits: **1**, Parity: **None**.
2. **Serialization (STM32 $\rightarrow$ Host):**
   Every 800ms, the STM32 serializes all values into a single text telemetry string followed by a structured JSON payload:
   ```text
   C1: 3.915V | C2: 3.826V | C3: 3.845V | C4: 3.848V || Amps: 0.000A || T1: 34.2C | T2: 33.9C || CO: 19.5 ppm || Vib: 0.002g
   ```
3. **Deserialization (Host $\rightarrow$ Python Daemon):**
   The host reads the serial pipeline, matching patterns using regular expressions (Regex) to extract variables:
   ```python
   volts = [re.search(r"C1:\s*([\d\.]+)", line), ...]
   ```

---

## 4. Machine Learning Pipeline & Data Merging

The host processor executes the AI diagnostics in real-time, matching predictions against incoming frames.

### A. ONNX Isolation Forest (Anomaly Detection)
* **Purpose:** Evaluates sensor signal integrity and determines the "Sensor Trust Index" (0-100%).
* **Structure:** Reads a $1 \times 8$ feature vector: `[C1_V, C2_V, C3_V, C4_V, Current, Temp, Vibration, Gas]`.
* **Mathematical Boundary:**
  Outputs an anomaly indicator ($y$):
  $$y = \text{sign}(\text{offset} - \text{score}(x))$$
  If $y = -1$, an outlier condition is detected.

### B. State of Health (SOH) PINN Estimator
* **Purpose:** Track capacitance degradation and capacity fade over time.
* **Math Model:** Combines neural network layers with physical constraints:
  $$\frac{d(\text{SOH})}{dt} = -k \cdot I^{n} \cdot \exp\left(-\frac{E_a}{R \cdot T_{\text{avg}}}\right)$$
  The model outputs the estimated remaining capacity retention percentage (`soh_pct`).

### C. Data Merger Process
When a telemetry line is parsed, the host combines physical metrics and ML outputs into a unified packet:

1. **Telemetry Parsing:** Compiles raw parameters.
2. **ONNX Inference:** Computes anomaly state and SOH metrics.
3. **Telemetry Packaging:** Merges all data into a single JSON diagnostic report:
   ```json
   {
     "timestamp": 1720468305,
     "device_id": "ev-uno-q-01",
     "cells": { "voltage_v": [3.915, 3.826, 3.845, 3.848], "temp_c": [34.2, 33.9] },
     "pack": { "current_a": 0.0, "vibration_g": 0.002, "gas_ppm": 19.5 },
     "ai_diagnostics": {
       "status": "ANOMALY_DETECTED",
       "soh_pct": 87.5,
       "severity": "HIGH",
       "confidence": 0.8900
     }
   }
   ```
4. **Hardware Feedbacks (Back-Channel IPC):**
   The host writes back to the STM32 via serial:
   `ML:ANOMALY,70,87\n`
   The STM32 parses this feedback and flashes corresponding warning colors on the local **8x13 LED Matrix** automatically.

---

## 5. Critical Thresholds & Safety Corner Cases

| Case Scenario | Analytical Condition / Formula | Severity Level | Actionable Target |
| :--- | :--- | :--- | :--- |
| **Thermal Runaway** | $T_1 > 65^\circ\text{C}$ or $T_2 > 65^\circ\text{C}$ | **CRITICAL** | Isolate pack, shut down load, sound buzzer |
| **Over-temperature alert**| $T_1 > 50^\circ\text{C}$ or $T_2 > 50^\circ\text{C}$ | **HIGH** | Reduce current draw, start exhaust fans |
| **Cell Voltage Imbalance**| $\max_i(V_{\text{cell}, i}) - \min_i(V_{\text{cell}, i}) > 0.35\text{V}$ | **MODERATE** | Enable shunt balance resistors |
| **Open Sensor Fault** | $T_{\text{read}} \le -127^\circ\text{C}$ or $V_{\text{gas\_sensor}} < 0.40\text{V}$ | **MODERATE** | Flag sensor disconnection anomaly |
| **Thermal Delta Delta** | $\left|T_1 - T_2\right| > 4^\circ\text{C}$ | **LOW** | Warn of local hot-spots |
| **Pack Current Spike** | $I_{\text{pack}} < -15\text{A}$ or $I_{\text{pack}} > 10\text{A}$ | **HIGH** | Check for charging overload or short circuit |
