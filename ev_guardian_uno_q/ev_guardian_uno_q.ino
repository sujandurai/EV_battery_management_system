/**
 * Arduino Uno Q — Calibrated 4-Cell Voltage, Current & Temperature Reader
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

void setup() {
  // Initialize UART console
  Serial.begin(115200);

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

  Serial.println("================================================");
  Serial.println(" Arduino Uno Q - Telemetry (Volt, Current, Temp)");
  Serial.println(" Calibrated Voltages (5.8159) active");
  Serial.println(" Calibrated Current (2.39V offset scaled)");
  Serial.println(" DS18B20 Temp Sensors: D4 (T1), D5 (T2)");
  Serial.println(" ADC Resolution: 14-Bit (0-16383)");
  Serial.println("================================================");
}

void loop() {
  // ─── CELL 1 ───
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
  int rawADC_Curr = analogRead(PIN_CURRENT);
  
  // Note: Pin A4 has an onboard 2:1 divider. We multiply by 2.0 to restore the real 2.39V sensor voltage.
  float currPinVolts = ((float)rawADC_Curr * (3.3f / 16383.0f)) * 2.0f;
  
  // Calculate current (Amperes) = (Pin_Voltage - Offset_Voltage) / Sensitivity
  float packCurrent = (currPinVolts - CURRENT_OFFSET_V) / CURRENT_SENSITIVITY;

  // Force actual 0.00A if very close to the zero-offset or disconnected (for stability)
  if (abs(currPinVolts - CURRENT_OFFSET_V) < 0.03f || currPinVolts < 0.2f) {
    packCurrent = 0.00f;
  }

  // NOTE: If you have wired your battery series pack taps directly to A0, A1, A2, A3, 
  // you can uncomment these lines to display the calculated individual cell voltages:
  /*
  float displayVolts1 = batteryVolts1;
  float displayVolts2 = batteryVolts2 - batteryVolts1;
  float displayVolts3 = batteryVolts3 - batteryVolts2;
  float displayVolts4 = batteryVolts4 - batteryVolts3;
  */
  // Otherwise, we print the raw battery voltage channels directly:
  float displayVolts1 = batteryVolts1;
  float displayVolts2 = batteryVolts2;
  float displayVolts3 = batteryVolts3;
  float displayVolts4 = batteryVolts4;

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
  // Read target temperatures (already converted since last loop cycle)
  float temp1 = readDS18B20(PIN_TEMP1);
  float temp2 = readDS18B20(PIN_TEMP2);
  
  // Start conversion for next loop read operation (takes ~94ms, completely non-blocking delay-free)
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
  Serial.println(" ppm");

  delay(800); // Output every 800ms
}
