/*
 * EV Guardian — Arduino UNO Q Firmware (STM32U585 / Zephyr RTOS)
 * ================================================================
 * File    : main.c
 * Target  : Arduino UNO Q (STM32U585CIT6 @ 160 MHz)
 * RTOS    : Zephyr v3.6
 * Purpose : Deterministic sensor acquisition and inter-core IPC bridge
 *
 * Thread Architecture:
 *   Thread A — Sensing & Filter Loop  (100 ms period, priority 5)
 *   Thread B — Display & Status Loop  (500 ms period, priority 7)
 *
 * IPC Bridge:
 *   Dual-port shared SRAM at 0x20000000 (volatile mapped)
 *   MPU (QRB2210) reads telemetry via hardware interconnect registers
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <math.h>
#include <string.h>
#include <stdio.h>

LOG_MODULE_REGISTER(ev_guardian, LOG_LEVEL_INF);

/* ─── Hardware Configuration ─────────────────────────────────────────────── */
#define ADC_NODE          DT_NODELABEL(adc1)
#define I2C_NODE          DT_NODELABEL(i2c1)
#define LED_GPIO_NODE     DT_NODELABEL(gpiob)

/* ADC Channel Assignments */
#define CH_CELL1   0    /* PA0 — Cell 1 Voltage divider output */
#define CH_CELL2   1    /* PA1 — Cell 2 Voltage divider output */
#define CH_CELL3   2    /* PA2 — Cell 3 Voltage divider output */
#define CH_CELL4   3    /* PA3 — Cell 4 Voltage divider output */
#define CH_CURRENT 4    /* PA4 — ACS712-05B current sense output */
#define CH_TEMP    5    /* PA5 — NTC Thermistor voltage divider */
#define CH_GAS     6    /* PA6 — MQ-2 gas sensor analog output */

/* MPU6050 I2C */
#define MPU6050_ADDR       0x68
#define MPU6050_REG_PWR    0x6B
#define MPU6050_REG_ACCEL  0x3B

/* ADC Configuration */
#define ADC_RESOLUTION  12
#define ADC_VREF_MV     3300  /* 3.3V internal reference */
#define ADC_ACQ_TICKS   160   /* Extended for high-impedance resistor dividers */
#define ADC_MAX_RAW     4095  /* 2^12 - 1 */

/* Sensor Calibration Constants */
#define CELL_SCALE_FACTOR   2.0f       /* Resistor divider ratio (10k/10k) */
#define ACS712_OFFSET_V     2.5f       /* Midpoint voltage at 0A */
#define ACS712_SENSITIVITY  0.185f     /* V/A for ACS712-05B */
#define NTC_PULLUP_OHM      10000.0f   /* 10kΩ pull-up resistor */
#define NTC_R0_OHM          10000.0f   /* NTC resistance at T0 */
#define NTC_T0_K            298.15f    /* T0 = 25°C in Kelvin */
#define NTC_BETA            3950.0f    /* NTC Beta coefficient */
#define MPU6050_SENSITIVITY 16384.0f  /* ±2g range: 16384 LSB/g */

/* Safety Thresholds */
#define CELL_VOLT_CRITICAL  2.5f    /* V  — Wire disconnect / over-discharge */
#define TEMP_CRITICAL_C     60.0f   /* °C — Pre-thermal runaway threshold */
#define GAS_CRITICAL_PPM    50.0f   /* ppm — Electrolyte off-gassing alert */

/* Thread Stack & Period */
#define SENSING_STACK_SIZE    2048
#define DISPLAY_STACK_SIZE    1024
#define SENSING_PERIOD_MS     100
#define DISPLAY_PERIOD_MS     500
#define FILTER_WINDOW_SIZE    5      /* Moving median filter taps */

/* ─── Shared IPC Memory Map ──────────────────────────────────────────────── */
/* Volatile-mapped dual-port SRAM region read by QRB2210 via hardware bus  */
#define IPC_BASE_ADDR       0x20000000UL
#define IPC_TELEMETRY_OFFS  0x0000      /* Raw telemetry block (256 bytes) */
#define IPC_TRUST_FLAG_OFFS 0x0100      /* Trust status flag (4 bytes) R/W  */
#define IPC_CONFIG_OFFS     0x0120      /* System config block (448 bytes)  */

typedef struct __attribute__((packed)) {
    uint32_t timestamp_ms;          /* Zephyr uptime in ms */
    float    cell_v[4];             /* Cell voltages in Volts */
    float    temp_c[4];             /* Cell temperatures in °C (all same NTC) */
    float    current_a;             /* Pack current in Amperes (+charge/-discharge) */
    float    vibration_g;           /* Vibration magnitude in G */
    float    gas_ppm;               /* Gas concentration approximation */
    uint8_t  fault_flags;           /* Bitmask: bit0=volt, bit1=temp, bit2=gas */
    uint8_t  node_status;           /* 0=OK, 1=SENSOR_FAULT, 2=COMM_FAIL */
    uint16_t checksum;              /* Simple XOR checksum of data */
} ipc_telemetry_t;

#define TRUST_STATUS_OK    0x00000001UL
#define TRUST_STATUS_FAULT 0x000000FFUL

volatile ipc_telemetry_t *ipc_telemetry = (volatile ipc_telemetry_t *)(IPC_BASE_ADDR + IPC_TELEMETRY_OFFS);
volatile uint32_t         *ipc_trust    = (volatile uint32_t *)(IPC_BASE_ADDR + IPC_TRUST_FLAG_OFFS);

/* ─── Device Handles ─────────────────────────────────────────────────────── */
static const struct device *adc_dev;
static const struct device *i2c_dev;
static const struct device *led_dev;

/* ─── ADC Channel Configs ────────────────────────────────────────────────── */
static const struct adc_channel_cfg adc_ch_cfgs[7] = {
    [CH_CELL1 ... CH_GAS] = {
        .gain             = ADC_GAIN_1,
        .reference        = ADC_REF_INTERNAL,
        .acquisition_time = ADC_ACQ_TIME(ADC_ACQ_TIME_TICKS, ADC_ACQ_TICKS),
        .differential     = 0,
    }
};
/* channel_id must be set individually */
static int16_t adc_buf[7];

/* ─── Filter State ───────────────────────────────────────────────────────── */
static float filter_bufs[7][FILTER_WINDOW_SIZE];
static int   filter_idx = 0;

/* Simple moving average filter (fast & deterministic for RTOS) */
static float mvg_avg(float *buf, float new_val, int channel) {
    buf[channel * FILTER_WINDOW_SIZE + (filter_idx % FILTER_WINDOW_SIZE)] = new_val;
    float sum = 0.0f;
    for (int i = 0; i < FILTER_WINDOW_SIZE; i++)
        sum += buf[channel * FILTER_WINDOW_SIZE + i];
    return sum / FILTER_WINDOW_SIZE;
}

/* ─── ADC Helpers ────────────────────────────────────────────────────────── */
static float adc_to_volts(int16_t raw) {
    return (float)raw * ADC_VREF_MV / ADC_MAX_RAW / 1000.0f;
}

static float read_adc_channel(uint8_t channel) {
    struct adc_sequence seq = {
        .channels    = BIT(channel),
        .buffer      = &adc_buf[channel],
        .buffer_size = sizeof(int16_t),
        .resolution  = ADC_RESOLUTION,
    };
    int ret = adc_read(adc_dev, &seq);
    if (ret < 0) {
        LOG_ERR("ADC ch%d read fail: %d", channel, ret);
        return 0.0f;
    }
    return adc_to_volts(adc_buf[channel]);
}

/* ─── Sensor Conversion Functions ────────────────────────────────────────── */
static float convert_cell_voltage(float v_adc) {
    /* Undo resistor divider: V_cell = V_adc * scale_factor */
    return v_adc * CELL_SCALE_FACTOR;
}

static float convert_current(float v_adc) {
    /* ACS712-05B: I = (V_out - V_ref) / sensitivity */
    return (v_adc - ACS712_OFFSET_V) / ACS712_SENSITIVITY;
}

static float convert_temperature(float v_adc) {
    /* NTC Steinhart-Hart simplified (Beta equation) */
    if (v_adc <= 0.01f || v_adc >= 3.29f) return -99.0f; /* Open/short */
    float r_ntc = NTC_PULLUP_OHM * ((ADC_VREF_MV / 1000.0f) / v_adc - 1.0f);
    float t_k   = 1.0f / ((1.0f / NTC_T0_K) + (1.0f / NTC_BETA) * logf(r_ntc / NTC_R0_OHM));
    return t_k - 273.15f;
}

static float convert_gas_ppm(float v_adc) {
    /* MQ-2 simplified piecewise linear approximation (calibrated in clean air) */
    /* Full RS/R0 ratio curve requires offline calibration; use linear estimate */
    float rs_r0 = (3.3f - v_adc) / v_adc; /* sensor resistance ratio */
    return rs_r0 * 100.0f;                  /* ~ppm equivalent */
}

/* ─── I2C MPU6050 ────────────────────────────────────────────────────────── */
static int mpu6050_init(void) {
    uint8_t wake_cmd[] = {MPU6050_REG_PWR, 0x00};  /* Clear sleep bit */
    return i2c_write(i2c_dev, wake_cmd, sizeof(wake_cmd), MPU6050_ADDR);
}

static float mpu6050_read_vibration_g(void) {
    uint8_t reg = MPU6050_REG_ACCEL;
    uint8_t data[6];
    int ret = i2c_write_read(i2c_dev, MPU6050_ADDR, &reg, 1, data, 6);
    if (ret < 0) {
        LOG_WRN("MPU6050 read fail: %d", ret);
        return 0.0f;
    }
    int16_t ax = (int16_t)((data[0] << 8) | data[1]);
    int16_t ay = (int16_t)((data[2] << 8) | data[3]);
    int16_t az = (int16_t)((data[4] << 8) | data[5]);
    float gx = ax / MPU6050_SENSITIVITY;
    float gy = ay / MPU6050_SENSITIVITY;
    float gz = az / MPU6050_SENSITIVITY;
    return sqrtf(gx*gx + gy*gy + gz*gz);
}

/* ─── Fault Detection ────────────────────────────────────────────────────── */
static uint8_t evaluate_faults(const ipc_telemetry_t *t) {
    uint8_t flags = 0;
    for (int i = 0; i < 4; i++) {
        if (t->cell_v[i] < CELL_VOLT_CRITICAL)  flags |= (1 << 0); /* Voltage fault */
        if (t->temp_c[i] > TEMP_CRITICAL_C)     flags |= (1 << 1); /* Thermal fault */
    }
    if (t->gas_ppm > GAS_CRITICAL_PPM)          flags |= (1 << 2); /* Gas fault */
    return flags;
}

static uint16_t compute_checksum(const uint8_t *data, size_t len) {
    uint16_t cs = 0;
    for (size_t i = 0; i < len - sizeof(uint16_t); i++) cs ^= data[i];
    return cs;
}

/* ─── Thread A: Sensing Loop (100 ms) ───────────────────────────────────── */
K_THREAD_STACK_DEFINE(sensing_stack, SENSING_STACK_SIZE);
static struct k_thread sensing_thread_data;

static void sensing_thread_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    static float filter_flat[7 * FILTER_WINDOW_SIZE] = {0};
    ipc_telemetry_t local_frame = {0};

    LOG_INF("Thread A: Sensing loop started (period=%dms)", SENSING_PERIOD_MS);

    while (1) {
        uint32_t t_start = k_uptime_get_32();
        filter_idx++;

        /* 1. Read all ADC channels */
        float v_cell1   = read_adc_channel(CH_CELL1);
        float v_cell2   = read_adc_channel(CH_CELL2);
        float v_cell3   = read_adc_channel(CH_CELL3);
        float v_cell4   = read_adc_channel(CH_CELL4);
        float v_current = read_adc_channel(CH_CURRENT);
        float v_temp    = read_adc_channel(CH_TEMP);
        float v_gas     = read_adc_channel(CH_GAS);

        /* 2. Read I2C vibration */
        float vibration = mpu6050_read_vibration_g();

        /* 3. Unit conversion */
        float c1 = convert_cell_voltage(v_cell1);
        float c2 = convert_cell_voltage(v_cell2);
        float c3 = convert_cell_voltage(v_cell3);
        float c4 = convert_cell_voltage(v_cell4);
        float curr = convert_current(v_current);
        float temp = convert_temperature(v_temp);
        float gas  = convert_gas_ppm(v_gas);

        /* 4. Moving-average filter (5-tap) */
        c1  = mvg_avg(filter_flat, c1,  0);
        c2  = mvg_avg(filter_flat, c2,  1);
        c3  = mvg_avg(filter_flat, c3,  2);
        c4  = mvg_avg(filter_flat, c4,  3);
        curr= mvg_avg(filter_flat, curr,4);
        temp= mvg_avg(filter_flat, temp,5);
        gas = mvg_avg(filter_flat, gas, 6);

        /* 5. Populate IPC frame */
        local_frame.timestamp_ms = k_uptime_get_32();
        local_frame.cell_v[0]    = c1;
        local_frame.cell_v[1]    = c2;
        local_frame.cell_v[2]    = c3;
        local_frame.cell_v[3]    = c4;
        local_frame.temp_c[0]    = temp;
        local_frame.temp_c[1]    = temp;  /* Single NTC → all cells same reading */
        local_frame.temp_c[2]    = temp;
        local_frame.temp_c[3]    = temp;
        local_frame.current_a    = curr;
        local_frame.vibration_g  = vibration;
        local_frame.gas_ppm      = gas;
        local_frame.fault_flags  = evaluate_faults(&local_frame);
        local_frame.node_status  = (local_frame.fault_flags == 0) ? 0x00 : 0x01;
        local_frame.checksum     = compute_checksum((uint8_t *)&local_frame,
                                                    sizeof(ipc_telemetry_t));

        /* 6. Atomic write to shared SRAM (word-aligned copy) */
        memcpy((void *)ipc_telemetry, &local_frame, sizeof(ipc_telemetry_t));

        LOG_DBG("C1=%.2fV C2=%.2fV C3=%.2fV C4=%.2fV T=%.1fC I=%.2fA G=%.0fppm V=%.3fG",
                c1, c2, c3, c4, temp, curr, gas, vibration);

        /* Precise period: compensate for loop execution time */
        uint32_t elapsed = k_uptime_get_32() - t_start;
        int32_t  sleep_ms = SENSING_PERIOD_MS - (int32_t)elapsed;
        if (sleep_ms > 0) k_msleep(sleep_ms);
    }
}

/* ─── Thread B: Display & Status Loop (500 ms) ───────────────────────────── */
K_THREAD_STACK_DEFINE(display_stack, DISPLAY_STACK_SIZE);
static struct k_thread display_thread_data;

/* LED Matrix pin assignments (8×3 = 24 GPIO for simplified row-column drive) */
#define LED_COL_COUNT  4
static const uint8_t led_col_pins[LED_COL_COUNT] = {0, 1, 2, 3}; /* PB0-PB3 */

static void led_set_pattern(bool is_fault, uint8_t cycle) {
    /* Simplified: PB0 = fault LED, PB1-PB3 = health bar LEDs */
    gpio_pin_set(led_dev, led_col_pins[0], is_fault ? (cycle % 2) : 0); /* Flash ! */
    if (!is_fault) {
        /* Scrolling charge animation based on cycle */
        for (int i = 1; i <= 3; i++) {
            gpio_pin_set(led_dev, led_col_pins[i], ((cycle / 2) % 4) >= i ? 1 : 0);
        }
    } else {
        for (int i = 1; i <= 3; i++) gpio_pin_set(led_dev, led_col_pins[i], 0);
    }
}

static void display_thread_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    /* Configure LED GPIO pins as outputs */
    for (int i = 0; i < LED_COL_COUNT; i++) {
        gpio_pin_configure(led_dev, led_col_pins[i], GPIO_OUTPUT_INACTIVE);
    }

    uint8_t anim_cycle = 0;
    LOG_INF("Thread B: Display loop started (period=%dms)", DISPLAY_PERIOD_MS);

    while (1) {
        /* Read trust status written by QRB2210 (from Snapdragon X analysis) */
        uint32_t trust = *ipc_trust;
        bool is_fault  = (trust == TRUST_STATUS_FAULT);
        bool cell_volt_low = (ipc_telemetry->fault_flags & 0x01) != 0;
        bool cell_temp_hi  = (ipc_telemetry->fault_flags & 0x02) != 0;

        /* Update LED display */
        led_set_pattern(is_fault, anim_cycle++);

        if (is_fault) {
            LOG_WRN("FAULT ACTIVE: flags=0x%02X trust=0x%08X",
                    ipc_telemetry->fault_flags, trust);
        }

        k_msleep(DISPLAY_PERIOD_MS);
    }
}

/* ─── main() ─────────────────────────────────────────────────────────────── */
int main(void) {
    LOG_INF("==================================================");
    LOG_INF("  EV Guardian Firmware v1.0");
    LOG_INF("  Target: Arduino UNO Q (STM32U585, Zephyr RTOS)");
    LOG_INF("==================================================");

    /* 1. Acquire device handles */
    adc_dev = DEVICE_DT_GET(ADC_NODE);
    i2c_dev = DEVICE_DT_GET(I2C_NODE);
    led_dev = DEVICE_DT_GET(LED_GPIO_NODE);

    if (!device_is_ready(adc_dev)) { LOG_ERR("ADC not ready!"); return -1; }
    if (!device_is_ready(i2c_dev)) { LOG_ERR("I2C not ready!"); return -1; }
    if (!device_is_ready(led_dev)) { LOG_ERR("LED GPIO not ready!"); return -1; }

    /* 2. Configure ADC channels */
    for (uint8_t ch = CH_CELL1; ch <= CH_GAS; ch++) {
        struct adc_channel_cfg cfg = adc_ch_cfgs[ch];
        cfg.channel_id = ch;
        int ret = adc_channel_setup(adc_dev, &cfg);
        if (ret < 0) {
            LOG_ERR("ADC ch%d config fail: %d", ch, ret);
            return -1;
        }
    }
    LOG_INF("ADC channels 0-6 configured (sampling=%d ticks)", ADC_ACQ_TICKS);

    /* 3. Initialize MPU6050 */
    int ret = mpu6050_init();
    if (ret < 0) {
        LOG_WRN("MPU6050 init warning: %d (continuing)", ret);
    } else {
        LOG_INF("MPU6050 I2C vibration sensor ready at 0x%02X", MPU6050_ADDR);
    }

    /* 4. Initialize IPC shared memory */
    memset((void *)ipc_telemetry, 0, sizeof(ipc_telemetry_t));
    *ipc_trust = TRUST_STATUS_OK;
    LOG_INF("IPC shared SRAM initialized at 0x%08X", IPC_BASE_ADDR);

    /* 5. Start Thread A — Sensing loop */
    k_thread_create(&sensing_thread_data, sensing_stack, SENSING_STACK_SIZE,
                    sensing_thread_entry, NULL, NULL, NULL,
                    5, 0, K_NO_WAIT);
    k_thread_name_set(&sensing_thread_data, "ev_sensing");

    /* 6. Start Thread B — Display loop */
    k_thread_create(&display_thread_data, display_stack, DISPLAY_STACK_SIZE,
                    display_thread_entry, NULL, NULL, NULL,
                    7, 0, K_NO_WAIT);
    k_thread_name_set(&display_thread_data, "ev_display");

    LOG_INF("Both threads launched. Entering kernel scheduler.");
    return 0;
}
