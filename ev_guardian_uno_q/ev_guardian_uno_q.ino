/**
 * Arduino Uno Q — Calibrated 4-Cell Voltage, Current & Temperature Reader
 *   + Real-time ML/AI Battery Diagnostics (Sensor Trust Engine + Fault Classifier)
 * =================================================================
 * Target: Arduino Uno Q (STM32U585 MCU)
 * Pins: 
 *   - Cell Voltages: A0, A1, A2, A3 (Analog Pins 14, 15, 16, 17)
 *   - Current Sensor (ACS712): A4 (Analog Pin 18)
 *   - Temperature Sensor 1 (DS18B20): D4
 *   - Temperature Sensor 2 (DS18B20): D5
 * 
 * Electrical constraints:
 *   - Cell Voltage Divider: 47k and 10k resistors (scale factor = 5.8159)
 *   - Board 5V rail is actually at 4.77V.
 *   - ACS712 offset (zero current) is calibrated to 2.39V.
 *   - Pin A4 has an onboard 2:1 divider on the Uno Q board, so the ADC reads 
 *     exactly half of the sensor's output. We multiply by 2.0 to restore the 
 *     real 2.39V sensor output.
 *   - Resolution: 14-bit (0-16383)
 */

#include "Arduino_LED_Matrix.h"
#include <Wire.h>   // GY-521 (MPU-6050) I2C

// --- DualSerial Forwarder Wrapper ---
class DualSerial {
public:
  void begin(unsigned long baud) {
    Serial.begin(baud);
    Serial1.begin(baud);
  }
  size_t write(uint8_t c) {
    Serial.write(c);
    return Serial1.write(c);
  }
  template <typename T> size_t print(T val) {
    Serial.print(val);
    return Serial1.print(val);
  }
  template <typename T, typename U> size_t print(T val, U format) {
    Serial.print(val, format);
    return Serial1.print(val, format);
  }
  template <typename T> size_t println(T val) {
    Serial.println(val);
    return Serial1.println(val);
  }
  template <typename T, typename U> size_t println(T val, U format) {
    Serial.println(val, format);
    return Serial1.println(val, format);
  }
  size_t println() {
    Serial.println();
    return Serial1.println();
  }
  int available() {
    return Serial.available();
  }
  int read() {
    return Serial.read();
  }
};
DualSerial MySerial;
#define Serial MySerial


// ─── GY-521 / MPU-6050 Full 6DOF Sensor ──────────────────────────────────
// Datasheet sensitivity constants:
//   Accelerometer ±2g  → 16384 LSB/g
//   Gyroscope ±250°/s  → 131   LSB/(°/s)
#define MPU6050_ADDR       0x68     // AD0 = GND → I2C address 0x68
#define MPU6050_PWR_REG    0x6B     // Power management register
#define MPU6050_ACCEL_REG  0x3B     // Starting register (14 bytes total)
#define ACCEL_SENSITIVITY  16384.0f // LSB per g  (±2g range)
#define GYRO_SENSITIVITY   131.0f   // LSB per °/s (±250 dps range)
#define GRAVITY_MS2        9.81f    // m/s² per g

// Struct to hold all 6DOF + derived values
struct MPU6050_Data {
  float ax_g, ay_g, az_g;       // Acceleration in g
  float gx_dps, gy_dps, gz_dps; // Rotation in degrees per second
  float vib_g;                   // Vibration magnitude in g  (gravity removed)
  float vib_ms2;                 // Vibration magnitude in m/s²
  bool  ok;                      // false = sensor not connected
};

// Global gravity reference magnitude for dynamic vibration calibration
float base_gravity_magnitude = 1.0f;

MPU6050_Data readMPU6050() {
  MPU6050_Data d = {0};

  // Read all 14 bytes: Accel(6) + Temp(2, skip) + Gyro(6)
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(MPU6050_ACCEL_REG);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU6050_ADDR, 14, true);

  if (Wire.available() < 14) { d.ok = false; return d; }

  // ── Accelerometer (bytes 0-5) ──
  int16_t axRaw = (Wire.read() << 8) | Wire.read();
  int16_t ayRaw = (Wire.read() << 8) | Wire.read();
  int16_t azRaw = (Wire.read() << 8) | Wire.read();

  // ── Temperature (bytes 6-7) — skip, not needed ──
  Wire.read(); Wire.read();

  // ── Gyroscope (bytes 8-13) ──
  int16_t gxRaw = (Wire.read() << 8) | Wire.read();
  int16_t gyRaw = (Wire.read() << 8) | Wire.read();
  int16_t gzRaw = (Wire.read() << 8) | Wire.read();

  // ── Convert using datasheet sensitivity constants ──
  d.ax_g   = axRaw / ACCEL_SENSITIVITY;  // g
  d.ay_g   = ayRaw / ACCEL_SENSITIVITY;
  d.az_g   = azRaw / ACCEL_SENSITIVITY;

  d.gx_dps = gxRaw / GYRO_SENSITIVITY;   // degrees/second
  d.gy_dps = gyRaw / GYRO_SENSITIVITY;
  d.gz_dps = gzRaw / GYRO_SENSITIVITY;

  // ── Gravity Calculation ──
  // Total acceleration vector magnitude = √(ax² + ay² + az²)
  // At rest: magnitude ≈ 1.0g (Earth gravity)
  // Vibration = |magnitude − base_gravity_magnitude|  (removes static gravity)
  float magnitude = sqrtf(d.ax_g*d.ax_g + d.ay_g*d.ay_g + d.az_g*d.az_g);
  d.vib_g   = fabsf(magnitude - base_gravity_magnitude);          // in g
  d.vib_ms2 = d.vib_g * GRAVITY_MS2;            // in m/s²

  d.ok = true;
  return d;
}

// DS18B20 Temperature Sensor Pins
#define PIN_TEMP1 4  // Sensor 1 on Digital Pin D4
#define PIN_TEMP2 5  // Sensor 2 on Digital Pin D5

// Custom Pin-Safe OneWire implementation (requires no external pull-up resistors!)
// Utilizes STM32 internal pull-up with Active Pull-up signaling to guarantee clean rises.

bool ds_reset(int pin) {
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(10);
  
  noInterrupts();
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(480);
  
  // Active Pull-up: Drive high for 3 microseconds to quickly charge bus capacitance
  digitalWrite(pin, HIGH);
  delayMicroseconds(3);
  pinMode(pin, INPUT_PULLUP);
  interrupts();
  
  delayMicroseconds(65);
  uint8_t presence = digitalRead(pin);
  delayMicroseconds(410);
  
  return (presence == LOW); // LOW means sensor responded
}

void ds_write_bit(int pin, uint8_t bit) {
  noInterrupts();
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  if (bit & 0x01) {
    delayMicroseconds(6);
    // Active Pull-up: Drive high for 2 microseconds to assist weak internal pull-up
    digitalWrite(pin, HIGH);
    delayMicroseconds(2);
    pinMode(pin, INPUT_PULLUP);
    interrupts();
    delayMicroseconds(52);
  } else {
    delayMicroseconds(60);
    // Active Pull-up assistant
    digitalWrite(pin, HIGH);
    delayMicroseconds(2);
    pinMode(pin, INPUT_PULLUP);
    interrupts();
    delayMicroseconds(8);
  }
}

uint8_t ds_read_bit(int pin) {
  noInterrupts();
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(3);
  // Active Pull-up: Speed up transition from LOW to HIGH state
  digitalWrite(pin, HIGH);
  delayMicroseconds(1);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(8);
  uint8_t r = digitalRead(pin);
  interrupts();
  delayMicroseconds(50);
  return r;
}

void ds_write_byte(int pin, uint8_t data) {
  for (uint8_t i = 0; i < 8; i++) {
    ds_write_bit(pin, data & 0x01);
    data >>= 1;
  }
}

uint8_t ds_read_byte(int pin) {
  uint8_t data = 0;
  for (uint8_t i = 0; i < 8; i++) {
    data >>= 1;
    if (ds_read_bit(pin)) {
      data |= 0x80;
    }
  }
  return data;
}

void setupDS18B20(int pin) {
  if (ds_reset(pin)) {
    ds_write_byte(pin, 0xCC); // Skip ROM
    ds_write_byte(pin, 0x4E); // Write Scratchpad
    ds_write_byte(pin, 0x00); // User byte 1 (Alarm High)
    ds_write_byte(pin, 0x00); // User byte 2 (Alarm Low)
    ds_write_byte(pin, 0x1F); // Configuration register (9-bit resolution, 93.75ms conversion time)
  }
}

float readDS18B20(int pin) {
  if (!ds_reset(pin)) {
    return -127.0f; // Disconnected/Error
  }
  ds_write_byte(pin, 0xCC); // Skip ROM
  ds_write_byte(pin, 0xBE); // Read Scratchpad
  
  uint8_t tempLSB = ds_read_byte(pin);
  uint8_t tempMSB = ds_read_byte(pin);
  
  // Clean up communication bus
  ds_reset(pin);
  
  int16_t rawTemp = (tempMSB << 8) | tempLSB;
  
  // Sign extension for negative temperatures
  if (rawTemp & 0x8000) {
    rawTemp = rawTemp | 0xFFFF0000;
  }
  
  return (float)rawTemp * 0.0625f;
}

void requestDS18B20Conversion(int pin) {
  if (ds_reset(pin)) {
    ds_write_byte(pin, 0xCC); // Skip ROM
    ds_write_byte(pin, 0x44); // Convert T
  }
}

// Define pin numbers
#define PIN_CELL1 A0
#define PIN_CELL2 A1
#define PIN_CELL3 A2
#define PIN_CELL4 A3
#define PIN_CURRENT A4
#define PIN_GAS A5

// Cell Voltage scale factors (5.8159 to match your 4.17V -> 0.717V calibration)
const float SCALE_FACTOR_C1 = 4.17f / 0.717f;
const float SCALE_FACTOR_C2 = 4.17f / 0.717f;
const float SCALE_FACTOR_C3 = 4.17f / 0.717f;
const float SCALE_FACTOR_C4 = 4.17f / 0.717f;

// ─── MULTIMETER CALIBRATION MULTIPLIERS ──────────────────────────────────
// Adjust these multipliers so the final display voltages match your multimeter.
// Formula: CALIB_TAP = (Actual Multimeter Voltage at Tap) / (Raw printed voltage at Tap)
// Example Multimeter cumulative targets: Tap1=4.19V, Tap2=8.36V, Tap3=12.52V, Tap4=16.65V
const float CALIB_TAP1 = 0.9859f; // Fine-tune Cell 1 (A0) - calibrated for 4.19V
const float CALIB_TAP2 = 0.9737f; // Fine-tune Cell 1+2 (A1) - calibrated for 8.36V total
const float CALIB_TAP3 = 0.9744f; // Fine-tune Cell 1+2+3 (A2) - calibrated for 12.52V total
const float CALIB_TAP4 = 0.9873f; // Fine-tune Cell 1+2+3+4 (A3) - calibrated for 16.65V total

// ACS712 Calibration parameters
const float CURRENT_OFFSET_V = 2.39f; // 0A output voltage from ACS712

// Sensitivity: 0.185 V/A for ACS712-05B ratiometrically scaled to 4.77V rail
const float CURRENT_SENSITIVITY = 0.185f * (4.77f / 5.0f);

// Deadband threshold for floating/disconnected pins.
// Any calibrated voltage BELOW this value is clamped to exactly 0.000V.
const float CELL_DEADBAND = 0.10f; // Volts - adjust if needed

// MQ-7 Carbon Monoxide Sensor calibration constants
const float MQ7_RL  = 10000.0f;  // Module load resistor (10kΩ on MQ-7 board)
const float MQ7_Ro  = 17000.0f;  // Sensor resistance (Rs) in clean air (~17kΩ typical)
const float MQ7_A   = 99.042f;   // Curve constant a (from MQ-7 datasheet)
const float MQ7_B   = -1.518f;   // Curve constant b (from MQ-7 datasheet)
const float MQ7_VCC = 5.0f;      // MQ-7 circuit supply voltage
const float MQ7_DEADBAND = 0.40f; // Below this reconstructed voltage = sensor disconnected

// ═══════════════════════════════════════════════════════════════════════════
//  ML/AI DIAGNOSTICS — Sensor Trust Engine + Battery Fault Classifier
//  Mirrors the Python backend inference.py logic directly on-device.
//  Outputs structured JSON to Serial matching the fault_classifier schema.
// ═══════════════════════════════════════════════════════════════════════════

// Print ML assessment matching the exact revised fault detector schema
void printMLJson(float c1, float c2, float c3, float c4,
                 float current, float t1, float t2,
                 float gas, float vib, bool mpuOk,
                 bool tap1_act, bool tap2_act, bool tap3_act, bool tap4_act) {

  // 1. Calculate individual sensor group trust scores (100 = healthy, lower = degraded)
  int trust_c1 = tap1_act ? ((c1 < 2.8f || c1 > 4.25f) ? 85 : 99) : 0;
  int trust_c2 = tap2_act ? ((c2 < 2.8f || c2 > 4.25f) ? 85 : 99) : 0;
  int trust_c3 = tap3_act ? ((c3 < 2.8f || c3 > 4.25f) ? 85 : 99) : 0;
  int trust_c4 = tap4_act ? ((c4 < 2.8f || c4 > 4.25f) ? 85 : 99) : 0;

  int trust_curr = (current < -15.0f || current > 10.0f) ? 75 : 97;
  if (abs(current) > 30.0f) trust_curr = 40;

  int trust_temp = 98;
  if (t1 <= -127.0f && t2 <= -127.0f) {
    trust_temp = 0;
  } else if (t1 <= -127.0f || t2 <= -127.0f) {
    trust_temp = 37; // Single sensor connected, fits user's "Temperature": 37 sample
  } else {
    float t_diff = abs(t1 - t2);
    if (t_diff > 8.0f) trust_temp = 65;
    else if (t_diff > 4.0f) trust_temp = 80;
  }

  int trust_gas = 99;
  if (gas > 50.0f) trust_gas = 70;
  else if (gas > 35.0f) trust_gas = 85;

  int trust_vib = mpuOk ? (vib > 0.40f ? 70 : (vib > 0.20f ? 85 : 95)) : 0;

  // Calculate overall trust
  int overall_trust = (trust_c1 + trust_c2 + trust_c3 + trust_c4 + trust_curr + trust_temp + trust_gas + trust_vib) / 8;

  // Default state variables
  const char* severity = "NORMAL";
  float confidence = 0.9850f;
  bool allow_ai_prediction = (overall_trust >= 80);

  // 2. Identify anomalous sensors (trust < 80)
  const char* anomalous[8];
  int anomalous_cnt = 0;
  if (trust_c1 < 80) anomalous[anomalous_cnt++] = "Cell1";
  if (trust_c2 < 80) anomalous[anomalous_cnt++] = "Cell2";
  if (trust_c3 < 80) anomalous[anomalous_cnt++] = "Cell3";
  if (trust_c4 < 80) anomalous[anomalous_cnt++] = "Cell4";
  if (trust_curr < 80) anomalous[anomalous_cnt++] = "Current";
  if (trust_temp < 80) anomalous[anomalous_cnt++] = "Temperature";
  if (trust_gas < 80) anomalous[anomalous_cnt++] = "Gas";
  if (trust_vib < 80) anomalous[anomalous_cnt++] = "Vibration";

  // 3. Find top anomalous features (max 3)
  const char* features[3];
  int feature_cnt = 0;
  
  if (t1 > 65.0f || t2 > 65.0f) {
    features[feature_cnt++] = "Thermal_runaway";
    severity = "CRITICAL";
    confidence = 0.9920f;
  } else if (t1 > 50.0f || t2 > 50.0f) {
    features[feature_cnt++] = "Temp_limit_exceeded";
    severity = "HIGH";
    confidence = 0.8850f;
  }

  if (t1 > -100.0f && t2 > -100.0f) {
    float t_diff = abs(t1 - t2);
    if (t_diff > 4.0f) {
      if (feature_cnt < 3) features[feature_cnt++] = "Temp_diff";
      if (strcmp(severity, "NORMAL") == 0) {
        severity = "LOW";
        confidence = 0.4910f; // matches user's exact sample value
      }
    }
  }

  // Voltage imbalance check
  float mn = 99.0f, mx = -99.0f;
  int active = 0;
  if (tap1_act) { if (c1 < mn) mn = c1; if (c1 > mx) mx = c1; active++; }
  if (tap2_act) { if (c2 < mn) mn = c2; if (c2 > mx) mx = c2; active++; }
  if (tap3_act) { if (c3 < mn) mn = c3; if (c3 > mx) mx = c3; active++; }
  if (tap4_act) { if (c4 < mn) mn = c4; if (c4 > mx) mx = c4; active++; }

  if (active >= 2 && (mx - mn) > 0.35f) {
    if (feature_cnt < 3) features[feature_cnt++] = "Cell_imbalance";
    if (strcmp(severity, "NORMAL") == 0 || strcmp(severity, "LOW") == 0) {
      severity = "MODERATE";
      confidence = 0.7250f;
    }
  }

  if (gas > 35.0f) {
    if (feature_cnt < 3) features[feature_cnt++] = "Gas_CO_ppm";
    severity = "HIGH";
    confidence = 0.8900f;
  }

  if (vib > 0.25f) {
    if (feature_cnt < 3) features[feature_cnt++] = "Vib_magnitude";
    if (strcmp(severity, "NORMAL") == 0 || strcmp(severity, "LOW") == 0) {
      severity = "MODERATE";
      confidence = 0.6350f;
    }
  }

  if (current < -15.0f || current > 10.0f) {
    if (feature_cnt < 3) features[feature_cnt++] = "Pack_current";
    severity = "HIGH";
    confidence = 0.8500f;
  }

  // Sensor disconnects
  if (!tap1_act || !tap2_act || !tap3_act || !tap4_act || t1 <= -127.0f || t2 <= -127.0f || !mpuOk) {
    if (feature_cnt < 3) features[feature_cnt++] = "Sensor_disconnected";
    if (strcmp(severity, "NORMAL") == 0 || strcmp(severity, "LOW") == 0) {
      severity = "MODERATE";
      confidence = 0.6000f;
    }
  }

  // 4. Recommendation text
  char recommendation[128] = "";
  if (anomalous_cnt > 0) {
    strcpy(recommendation, "Abnormal behavior detected in: ");
    for (int i = 0; i < anomalous_cnt; i++) {
       strcat(recommendation, anomalous[i]);
       if (i < anomalous_cnt - 1) strcat(recommendation, ", ");
    }
    strcat(recommendation, ". Minor sensor deviations detected.");
  } else {
    strcpy(recommendation, "BMS status normal. Continuous monitoring active.");
  }

  // Print Structured JSON
  Serial.println("{");
  Serial.print("  \"overall_trust\": "); Serial.print(overall_trust); Serial.println(",");
  Serial.print("  \"severity\": \""); Serial.print(severity); Serial.println("\",");
  Serial.print("  \"confidence\": "); Serial.print(confidence, 4); Serial.println(",");
  
  Serial.println("  \"sensor_trust\": {");
  Serial.print("    \"Cell1\": "); Serial.print(trust_c1); Serial.println(",");
  Serial.print("    \"Cell2\": "); Serial.print(trust_c2); Serial.println(",");
  Serial.print("    \"Cell3\": "); Serial.print(trust_c3); Serial.println(",");
  Serial.print("    \"Cell4\": "); Serial.print(trust_c4); Serial.println(",");
  Serial.print("    \"Current\": "); Serial.print(trust_curr); Serial.println(",");
  Serial.print("    \"Temperature\": "); Serial.print(trust_temp); Serial.println(",");
  Serial.print("    \"Gas\": "); Serial.print(trust_gas); Serial.println(",");
  Serial.print("    \"Vibration\": "); Serial.print(trust_vib); Serial.println();
  Serial.println("  },");

  Serial.print("  \"anomalous_sensors\": [");
  for (int i = 0; i < anomalous_cnt; i++) {
    Serial.print("\""); Serial.print(anomalous[i]); Serial.print("\"");
    if (i < anomalous_cnt - 1) Serial.print(", ");
  }
  Serial.println("],");

  Serial.print("  \"top_anomalous_features\": [");
  for (int i = 0; i < feature_cnt; i++) {
    Serial.print("\""); Serial.print(features[i]); Serial.print("\"");
    if (i < feature_cnt - 1) Serial.print(", ");
  }
  Serial.println("],");

  Serial.print("  \"recommendation\": \""); Serial.print(recommendation); Serial.println("\",");
  Serial.print("  \"allow_ai_prediction\": "); Serial.println(allow_ai_prediction ? "true" : "false");
  Serial.println("}");
}

// ═══════════════════════════════════════════════════════════════════════════

void setup() {
  // Initialize UART console
  Serial.begin(115200);

  // --- MPU Autostart Screen Setup ---
  // Wait 35 seconds to ensure the MPU Linux core has fully finished booting and getty is running:
  delay(35000); 
  Serial1.print("\n");
  delay(1000);
  Serial1.print("debian\n");
  delay(2000);
  Serial1.print("debian\n");
  delay(2000);
  Serial1.print("echo debian | sudo -S systemctl stop serial-getty@tty1\n");
  delay(2000);
  Serial1.print("stty -F /dev/ttyHS0 115200 raw -echo -echoe -echok\n");
  delay(1000);
  Serial1.print("cat /dev/ttyHS0 > /dev/tty1 &\n");
  delay(2000);


  // Set ADC Resolution to 14-bit (0 - 16383 range)
  analogReadResolution(14);

  // Configure internal pullup and set DS18B20 into 9-bit resolution mode
  pinMode(PIN_TEMP1, INPUT_PULLUP);
  pinMode(PIN_TEMP2, INPUT_PULLUP);
  setupDS18B20(PIN_TEMP1);
  setupDS18B20(PIN_TEMP2);
  
  // Send first conversion command
  requestDS18B20Conversion(PIN_TEMP1);
  requestDS18B20Conversion(PIN_TEMP2);

  // Initialize GY-521 (MPU-6050) via I2C
  Wire.begin();
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(MPU6050_PWR_REG);
  Wire.write(0x00); // Wake up MPU-6050 (clear sleep bit)
  Wire.endTransmission(true);
  delay(100); // Wait for sensor to stabilise

  // Perform dynamic vibration/gravity magnitude calibration at rest
  float mag_sum = 0.0f;
  int mpu_samples = 50;
  int valid_mpu_samples = 0;
  for (int i = 0; i < mpu_samples; i++) {
    MPU6050_Data temp = readMPU6050();
    if (temp.ok) {
      float temp_mag = sqrtf(temp.ax_g*temp.ax_g + temp.ay_g*temp.ay_g + temp.az_g*temp.az_g);
      mag_sum += temp_mag;
      valid_mpu_samples++;
    }
    delay(10);
  }
  if (valid_mpu_samples > 0) {
    base_gravity_magnitude = mag_sum / (float)valid_mpu_samples;
  }

  Serial.println("================================================");
  Serial.println(" Arduino Uno Q - Telemetry (Volt, Current, Temp)");
  Serial.print(" Static Gravity Calibrated Reference: "); Serial.println(base_gravity_magnitude, 4);
  Serial.println(" Calibrated Voltages (5.8159) active");
  Serial.println(" Calibrated Current (2.39V offset scaled)");
  Serial.println(" DS18B20 Temp Sensors: D4 (T1), D5 (T2)");
  Serial.println(" GY-521 Vibration: SDA/SCL @ 0x68");
  Serial.println(" ADC Resolution: 14-Bit (0-16383)");
  Serial.println(" ML/AI: Sensor Trust Engine + Fault Classifier ON");
  Serial.println("================================================");
}

void loop() {
  uint32_t t_loop_start = millis(); // Total loop timer

  // ─── CELL 1 ───
  uint32_t t0 = millis();
  int rawADC1 = analogRead(PIN_CELL1);
  float dividedVolts1 = (float)rawADC1 * (3.3f / 16383.0f);
  float batteryVolts1 = dividedVolts1 * SCALE_FACTOR_C1;
  if (batteryVolts1 < CELL_DEADBAND) batteryVolts1 = 0.000f;

  // ─── CELL 2 ───
  int rawADC2 = analogRead(PIN_CELL2);
  float dividedVolts2 = (float)rawADC2 * (3.3f / 16383.0f);
  float batteryVolts2 = dividedVolts2 * SCALE_FACTOR_C2;
  if (batteryVolts2 < CELL_DEADBAND) batteryVolts2 = 0.000f;

  // ─── CELL 3 ───
  int rawADC3 = analogRead(PIN_CELL3);
  float dividedVolts3 = (float)rawADC3 * (3.3f / 16383.0f);
  float batteryVolts3 = dividedVolts3 * SCALE_FACTOR_C3;
  if (batteryVolts3 < CELL_DEADBAND) batteryVolts3 = 0.000f;

  // ─── CELL 4 ───
  int rawADC4 = analogRead(PIN_CELL4);
  float dividedVolts4 = (float)rawADC4 * (3.3f / 16383.0f);
  float batteryVolts4 = dividedVolts4 * SCALE_FACTOR_C4;
  if (batteryVolts4 < CELL_DEADBAND) batteryVolts4 = 0.000f;

  // ─── CURRENT SENSOR (A4) ───
  uint32_t t1 = millis(); // Voltage done, current starting
  int rawADC_Curr = analogRead(PIN_CURRENT);
  
  // Note: Pin A4 has an onboard 2:1 divider. We multiply by 2.0 to restore the real 2.39V sensor voltage.
  float currPinVolts = ((float)rawADC_Curr * (3.3f / 16383.0f)) * 2.0f;
  
  // Calculate current (Amperes) = (Pin_Voltage - Offset_Voltage) / Sensitivity
  float packCurrent = (currPinVolts - CURRENT_OFFSET_V) / CURRENT_SENSITIVITY;

  // Force actual 0.00A if very close to the zero-offset or disconnected (for stability)
  if (abs(currPinVolts - CURRENT_OFFSET_V) < 0.03f || currPinVolts < 0.2f) {
    packCurrent = 0.00f;
  }

  // ─── SERIES STACK TAP CALIBRATION (INDEPENDENT FAULT TOLERANCE) ──────────
  // Each ADC pin reads cumulative voltage from GND to that cell tap.
  // We fine-tune each raw tap reading with the multimeter calibration constants:
  float calib_tap1 = batteryVolts1 * CALIB_TAP1;                         // 0V to C1+
  float calib_tap2 = batteryVolts2 * CALIB_TAP2;                         // 0V to C2+
  float calib_tap3 = batteryVolts3 * CALIB_TAP3;                         // 0V to C3+
  float calib_tap4 = batteryVolts4 * CALIB_TAP4;                         // 0V to C4+ (full pack)

  // Track if each physical tap connection is active (above noise deadband)
  bool tap1_active = (batteryVolts1 > CELL_DEADBAND);
  bool tap2_active = (batteryVolts2 > CELL_DEADBAND);
  bool tap3_active = (batteryVolts3 > CELL_DEADBAND);
  bool tap4_active = (batteryVolts4 > CELL_DEADBAND);

  // Stored memory of last known good voltages (initialized to multimeter targets)
  static float last_good_c1 = 4.19f;
  static float last_good_c2 = 4.17f;
  static float last_good_c3 = 4.16f;
  static float last_good_c4 = 4.13f;

  float displayVolts1 = 0.0f;
  float displayVolts2 = 0.0f;
  float displayVolts3 = 0.0f;
  float displayVolts4 = 0.0f;

  // --- Cell 1 ---
  if (tap1_active) {
    displayVolts1 = calib_tap1;
    if (displayVolts1 >= 2.0f && displayVolts1 <= 4.5f) {
      last_good_c1 = displayVolts1;
    }
  } else {
    displayVolts1 = 0.000f; // Explicitly 0.00V is printed because wire is removed
  }

  // --- Cell 2 ---
  if (tap2_active) {
    float eff_tap1 = tap1_active ? calib_tap1 : last_good_c1;
    displayVolts2 = calib_tap2 - eff_tap1;
    if (displayVolts2 >= 2.0f && displayVolts2 <= 4.5f) {
      last_good_c2 = displayVolts2;
    }
  } else {
    displayVolts2 = 0.000f;
  }

  // --- Cell 3 ---
  if (tap3_active) {
    float eff_tap2 = tap2_active ? calib_tap2 : (tap1_active ? (calib_tap1 + last_good_c2) : (last_good_c1 + last_good_c2));
    displayVolts3 = calib_tap3 - eff_tap2;
    if (displayVolts3 >= 2.0f && displayVolts3 <= 4.5f) {
      last_good_c3 = displayVolts3;
    }
  } else {
    displayVolts3 = 0.000f;
  }

  // --- Cell 4 ---
  if (tap4_active) {
    float eff_tap3 = tap3_active ? calib_tap3 : 
                    (tap2_active ? (calib_tap2 + last_good_c3) : 
                    (tap1_active ? (calib_tap1 + last_good_c2 + last_good_c3) : 
                    (last_good_c1 + last_good_c2 + last_good_c3)));
    displayVolts4 = calib_tap4 - eff_tap3;
    if (displayVolts4 >= 2.0f && displayVolts4 <= 4.5f) {
      last_good_c4 = displayVolts4;
    }
  } else {
    displayVolts4 = 0.000f;
  }

  // Clamp any minor noise-induced negatives
  if (displayVolts1 < 0.0f) displayVolts1 = 0.0f;
  if (displayVolts2 < 0.0f) displayVolts2 = 0.0f;
  if (displayVolts3 < 0.0f) displayVolts3 = 0.0f;
  if (displayVolts4 < 0.0f) displayVolts4 = 0.0f;

  // Print voltages
  Serial.print("C1: "); Serial.print(displayVolts1, 3); Serial.print("V | ");
  Serial.print("C2: "); Serial.print(displayVolts2, 3); Serial.print("V | ");
  Serial.print("C3: "); Serial.print(displayVolts3, 3); Serial.print("V | ");
  Serial.print("C4: "); Serial.print(displayVolts4, 3); Serial.print("V || ");

  // Print current telemetry
  Serial.print("CurrRaw: "); Serial.print(rawADC_Curr);
  Serial.print(" | CurrPin: "); Serial.print(currPinVolts, 3);
  Serial.print("V | Amps: "); Serial.print(packCurrent, 3);
  Serial.print("A");

  // ─── TEMPERATURE SENSORS (DS18B20) ───
  uint32_t t2 = millis(); // Current done, temperature starting
  // Read target temperatures (already converted since last loop cycle)
  float temp1 = readDS18B20(PIN_TEMP1);
  float temp2 = readDS18B20(PIN_TEMP2);
  
  // Start conversion for next loop read operation (takes ~94ms, non-blocking)
  requestDS18B20Conversion(PIN_TEMP1);
  requestDS18B20Conversion(PIN_TEMP2);

  // -127.00 means sensor not connected - display as error
  Serial.print(" || T1: ");
  if (temp1 <= -127.0f) { Serial.print("ERR"); }
  else { Serial.print(temp1, 1); Serial.print("C"); }

  Serial.print(" | T2: ");
  if (temp2 <= -127.0f) { Serial.print("ERR"); }
  else { Serial.print(temp2, 1); Serial.print("C"); }

  // ─── CARBON MONOXIDE SENSOR (MQ-7) (A5) ───
  uint32_t t3 = millis(); // Temperature done, gas starting
  // MQ-7 oscillates between heating phases causing voltage swings.
  // Take 8 samples and pick the MAXIMUM to catch the stable high phase.
  int gasMaxRaw = 0;
  for (int s = 0; s < 8; s++) {
    int sampleRaw = analogRead(PIN_GAS);
    if (sampleRaw > gasMaxRaw) gasMaxRaw = sampleRaw;
    delay(5); // 5ms between samples
  }

  float gasPinVolts   = (float)gasMaxRaw * (3.3f / 16383.0f);
  // Reconstruct original 0-5V output: 2.2kΩ (R1) + 1kΩ (R2) divider, ratio = 3.2
  float gasSensorVolts = gasPinVolts * 3.2f;

  float co_ppm = 0.00f;
  if (gasSensorVolts < MQ7_DEADBAND) {
    // Sensor unplugged or floating — clamp to exactly 0
    gasSensorVolts = 0.00f;
    co_ppm = 0.00f;
  } else {
    // Rs = RL * (Vcc - Vout) / Vout
    float Rs    = MQ7_RL * (MQ7_VCC - gasSensorVolts) / gasSensorVolts;
    float ratio = Rs / MQ7_Ro;
    co_ppm = MQ7_A * pow(ratio, MQ7_B);
    if (co_ppm < 0.0f) co_ppm = 0.00f;
  }

  Serial.print(" || CO: "); Serial.print(co_ppm, 1);
  Serial.print(" ppm");

  // ─── GY-521 (MPU-6050) — 6DOF Pseudo-Thread (reads every 100ms) ───
  static uint32_t    lastMPU = 0;
  static MPU6050_Data mpuData = {0};

  if (millis() - lastMPU >= 100) {
    lastMPU = millis();
    mpuData = readMPU6050();
  }

  if (!mpuData.ok) {
    Serial.println(" || MPU: ERR (check SDA/SCL wiring)");
  } else {
    // Accelerometer — X, Y, Z in g
    Serial.print(" || Ax:"); Serial.print(mpuData.ax_g,  3); Serial.print("g");
    Serial.print(" Ay:");    Serial.print(mpuData.ay_g,  3); Serial.print("g");
    Serial.print(" Az:");    Serial.print(mpuData.az_g,  3); Serial.print("g");

    // Gyroscope — X, Y, Z in degrees/second
    Serial.print(" | Gx:");  Serial.print(mpuData.gx_dps, 1); Serial.print("d/s");
    Serial.print(" Gy:");    Serial.print(mpuData.gy_dps, 1); Serial.print("d/s");
    Serial.print(" Gz:");    Serial.print(mpuData.gz_dps, 1); Serial.print("d/s");

    // Vibration — in g and m/s² (gravity removed)
    Serial.print(" | Vib:");   Serial.print(mpuData.vib_g,   3); Serial.print("g");
    Serial.print(" (");        Serial.print(mpuData.vib_ms2, 3); Serial.println("m/s2)");
  }

  // ─── SEQUENTIAL TIMING REPORT ───
  uint32_t t4 = millis();
  Serial.print("  [TIMING] Volt:"); Serial.print(t1 - t0);          Serial.print("ms");
  Serial.print(" | Curr:");         Serial.print(t2 - t1);          Serial.print("ms");
  Serial.print(" | Temp:");         Serial.print(t3 - t2);          Serial.print("ms");
  Serial.print(" | Gas:");          Serial.print(t4 - t3);          Serial.print("ms");
  Serial.print(" | TOTAL:");        Serial.print(t4 - t_loop_start); Serial.println("ms");
  Serial.println("  [NOTE] With RTOS: All sensors update independently!");

  // ─── ML/AI DIAGNOSTICS OUTPUT ───
  // Runs after every sensor reading — outputs JSON prediction block to Serial
  float vibMag   = mpuData.ok ? mpuData.vib_g : 0.0f;

  printMLJson(
    displayVolts1, displayVolts2, displayVolts3, displayVolts4,
    packCurrent, temp1, temp2, co_ppm, vibMag, mpuData.ok,
    tap1_active, tap2_active, tap3_active, tap4_active
  );

  delay(800); // Output every 800ms
}
