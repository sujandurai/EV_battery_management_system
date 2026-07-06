/*
 * EV Guardian — Arduino UNO Q Firmware (STM32U585 / Zephyr RTOS)
 * ================================================================
 * File    : main.c
 * Target  : Arduino UNO Q (STM32U585 @ 160 MHz)
 * RTOS    : Zephyr v3.6
 *
 * Thread Architecture (4 threads, all running concurrently):
 *   Thread A — Voltage & Current  (50 ms period,  priority 2)  HIGH
 *   Thread B — Temperature        (100 ms period, priority 4)  MED
 *   Thread C — CO Gas (MQ-7)      (500 ms period, priority 6)  LOW
 *   Thread D — Serial Print       (100 ms period, priority 8)  OUTPUT
 *
 * All threads write to a shared telemetry struct protected by a mutex.
 * Thread D reads shared struct and prints to serial (UART console).
 *
 * Confirmed Calibration Constants (verified on hardware):
 *   - Voltage scale factor: 5.8159 (4.17V / 0.717V divider)
 *   - ADC resolution: 14-bit (0-16383), VREF = 3.3V
 *   - ACS712 zero-current offset: 2.39V, sensitivity: 0.17649 V/A
 *   - A4 onboard PCB 2:1 divider — multiply by 2.0 to restore real voltage
 *   - DS18B20: active pull-up internal OneWire, 9-bit (0.0625°C/LSB)
 *   - MQ-7 CO: 2.2kΩ+1kΩ divider (ratio 3.2), Ro=17kΩ, A=99.042, B=-1.518
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <math.h>
#include <string.h>
#include <stdio.h>

LOG_MODULE_REGISTER(ev_guardian, LOG_LEVEL_INF);

/* ─── Pin / Channel Definitions ──────────────────────────────────────────── */
#define ADC_NODE    DT_NODELABEL(adc1)

/* ADC channel indexes (match arduino_uno_q overlay PA4=A0...PC0=A5) */
#define CH_CELL1    0   /* A0 — PA4 */
#define CH_CELL2    1   /* A1 — PA5 */
#define CH_CELL3    2   /* A2 — PA6 */
#define CH_CELL4    3   /* A3 — PA7 */
#define CH_CURRENT  4   /* A4 — PC1 (has onboard 2:1 divider) */
#define CH_GAS      5   /* A5 — PC0 (MQ-7 via 2.2k+1k divider) */

/* DS18B20 GPIO pins (PA12 = D4, PA11 = D5) */
#define TEMP1_GPIO_NODE  DT_NODELABEL(gpioa)
#define TEMP1_PIN        12   /* D4 = PA12 */
#define TEMP2_PIN        11   /* D5 = PA11 */

/* ─── ADC Configuration ───────────────────────────────────────────────────── */
#define ADC_RESOLUTION   14
#define ADC_VREF_MV      3300
#define ADC_MAX_RAW      16383   /* 2^14 - 1 */
#define ADC_ACQ_TICKS    160

/* ─── Confirmed Calibration Constants ────────────────────────────────────── */
/* VOLTAGE — DO NOT CHANGE (verified: 4.17V / 0.717V = 5.8159) */
#define CELL_SCALE      5.8159f
#define CELL_DEADBAND   0.10f    /* Volts — below this = disconnected = 0.000V */

/* CURRENT — DO NOT CHANGE (verified: ACS712-05B on 4.77V rail) */
#define CURR_OFFSET     2.39f    /* 0A output voltage (at real sensor pin, after ×2) */
#define CURR_SENS       (0.185f * (4.77f / 5.0f))  /* = 0.17649 V/A */
#define CURR_DEADBAND   0.03f    /* Volts — zero-lock band */

/* MQ-7 CO — DO NOT CHANGE (datasheet curve + 2.2k+1k divider) */
#define MQ7_RL          10000.0f
#define MQ7_Ro          17000.0f
#define MQ7_A           99.042f
#define MQ7_B           -1.518f
#define MQ7_VCC         5.0f
#define MQ7_DEADBAND    0.40f    /* Volts — below this = sensor disconnected */
#define MQ7_DIVIDER_R   3.2f     /* (2.2k + 1.0k) / 1.0k */

/* DS18B20 */
#define DS18B20_BITS_PER_C  0.0625f   /* 9-bit: 1 LSB = 0.0625°C */

/* ─── Thread Timing ───────────────────────────────────────────────────────── */
#define THREAD_A_PERIOD_MS   50    /* Voltage + Current */
#define THREAD_B_PERIOD_MS   100   /* Temperature (DS18B20) */
#define THREAD_C_PERIOD_MS   500   /* MQ-7 CO Gas */
#define THREAD_D_PERIOD_MS   100   /* Serial print */

/* ─── Thread Stack Sizes ──────────────────────────────────────────────────── */
#define STACK_A   2048
#define STACK_B   2048
#define STACK_C   1024
#define STACK_D   1024

/* ─── Shared Telemetry Struct ─────────────────────────────────────────────── */
static struct k_mutex telemetry_mutex;

typedef struct {
    float cell_v[4];      /* C1..C4 in Volts */
    float current_a;      /* Pack current in Amperes */
    float temp_c[2];      /* T1, T2 in Celsius (-127 = ERR) */
    float co_ppm;         /* CO concentration in PPM (0 = disconnected) */
    uint32_t ts_volt_ms;  /* Last voltage update timestamp */
    uint32_t ts_temp_ms;  /* Last temperature update timestamp */
    uint32_t ts_gas_ms;   /* Last gas update timestamp */
} ev_telemetry_t;

static ev_telemetry_t g_telem = {
    .cell_v    = {0},
    .current_a = 0.0f,
    .temp_c    = {-127.0f, -127.0f},
    .co_ppm    = 0.0f,
};

/* ─── ADC Device ──────────────────────────────────────────────────────────── */
static const struct device *adc_dev;
static int16_t adc_sample_buf;

static float read_adc_channel(uint8_t ch) {
    struct adc_channel_cfg cfg = {
        .gain             = ADC_GAIN_1,
        .reference        = ADC_REF_INTERNAL,
        .acquisition_time = ADC_ACQ_TIME(ADC_ACQ_TIME_TICKS, ADC_ACQ_TICKS),
        .differential     = 0,
        .channel_id       = ch,
    };
    adc_channel_setup(adc_dev, &cfg);

    struct adc_sequence seq = {
        .channels    = BIT(ch),
        .buffer      = &adc_sample_buf,
        .buffer_size = sizeof(adc_sample_buf),
        .resolution  = ADC_RESOLUTION,
    };
    if (adc_read(adc_dev, &seq) < 0) return 0.0f;
    float v = (float)adc_sample_buf * (ADC_VREF_MV / 1000.0f) / ADC_MAX_RAW;
    return (v < 0.0f) ? 0.0f : v;
}

/* ─── Software OneWire for DS18B20 ───────────────────────────────────────── */
static const struct device *temp_gpio_dev;

static void ow_drive_low(uint8_t pin) {
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_OUTPUT_LOW);
}

static void ow_release(uint8_t pin) {
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
}

static int ow_read_bit(uint8_t pin) {
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_OUTPUT_LOW);
    k_busy_wait(3);
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
    k_busy_wait(9);
    int bit = gpio_pin_get(temp_gpio_dev, pin);
    k_busy_wait(50);
    return bit;
}

static void ow_write_bit(uint8_t pin, int bit) {
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_OUTPUT_LOW);
    if (bit) {
        k_busy_wait(6);
        gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
        k_busy_wait(54);
    } else {
        k_busy_wait(60);
        gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
        k_busy_wait(10);
    }
}

static bool ow_reset(uint8_t pin) {
    ow_drive_low(pin);
    k_busy_wait(480);
    ow_release(pin);
    k_busy_wait(70);
    int presence = !gpio_pin_get(temp_gpio_dev, pin); /* sensor pulls low */
    k_busy_wait(410);
    return (bool)presence;
}

static void ow_write_byte(uint8_t pin, uint8_t data) {
    for (int i = 0; i < 8; i++) {
        ow_write_bit(pin, data & 0x01);
        data >>= 1;
    }
}

static uint8_t ow_read_byte(uint8_t pin) {
    uint8_t val = 0;
    for (int i = 0; i < 8; i++) {
        val >>= 1;
        if (ow_read_bit(pin)) val |= 0x80;
    }
    return val;
}

/* Start conversion (non-blocking — runs in background during k_sleep) */
static void ds_start_conversion(uint8_t pin) {
    if (ow_reset(pin)) {
        ow_write_byte(pin, 0xCC); /* Skip ROM */
        ow_write_byte(pin, 0x44); /* Convert T */
    }
}

/* Read scratchpad result (call after >= 94ms for 9-bit) */
static float ds_read_temp(uint8_t pin) {
    if (!ow_reset(pin)) return -127.0f;
    ow_write_byte(pin, 0xCC); /* Skip ROM */
    ow_write_byte(pin, 0xBE); /* Read scratchpad */
    uint8_t lsb = ow_read_byte(pin);
    uint8_t msb = ow_read_byte(pin);
    ow_reset(pin);
    int16_t raw = ((int16_t)msb << 8) | lsb;
    return (float)raw * DS18B20_BITS_PER_C;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * THREAD A — Voltage & Current  (50ms, priority 2 — HIGHEST)
 * ═══════════════════════════════════════════════════════════════════════════ */
K_THREAD_STACK_DEFINE(stack_a, STACK_A);
static struct k_thread thread_a_data;

static void thread_a_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    while (1) {
        float v[4], curr;

        /* ── Read all 4 cells ── */
        for (int i = 0; i < 4; i++) {
            float adc_v = read_adc_channel(CH_CELL1 + i);
            float cal   = adc_v * CELL_SCALE;
            v[i] = (cal < CELL_DEADBAND) ? 0.000f : cal;
        }

        /* ── Read current (A4 has onboard 2:1 divider → multiply by 2) ── */
        float curr_pin = read_adc_channel(CH_CURRENT) * 2.0f;
        if (fabsf(curr_pin - CURR_OFFSET) < CURR_DEADBAND || curr_pin < 0.2f) {
            curr = 0.00f;
        } else {
            curr = (curr_pin - CURR_OFFSET) / CURR_SENS;
        }

        /* ── Write to shared telemetry (mutex protected) ── */
        k_mutex_lock(&telemetry_mutex, K_FOREVER);
        for (int i = 0; i < 4; i++) g_telem.cell_v[i] = v[i];
        g_telem.current_a  = curr;
        g_telem.ts_volt_ms = k_uptime_get_32();
        k_mutex_unlock(&telemetry_mutex);

        k_sleep(K_MSEC(THREAD_A_PERIOD_MS));
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * THREAD B — DS18B20 Temperature (100ms, priority 4)
 * ═══════════════════════════════════════════════════════════════════════════ */
K_THREAD_STACK_DEFINE(stack_b, STACK_B);
static struct k_thread thread_b_data;

static void thread_b_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    /* Prime the first conversion before first read */
    ds_start_conversion(TEMP1_PIN);
    ds_start_conversion(TEMP2_PIN);
    k_sleep(K_MSEC(THREAD_B_PERIOD_MS));

    while (1) {
        /* Read result from LAST conversion */
        float t1 = ds_read_temp(TEMP1_PIN);
        float t2 = ds_read_temp(TEMP2_PIN);

        /* Start next conversion (takes ~94ms — finishes during next sleep) */
        ds_start_conversion(TEMP1_PIN);
        ds_start_conversion(TEMP2_PIN);

        k_mutex_lock(&telemetry_mutex, K_FOREVER);
        g_telem.temp_c[0]  = t1;
        g_telem.temp_c[1]  = t2;
        g_telem.ts_temp_ms = k_uptime_get_32();
        k_mutex_unlock(&telemetry_mutex);

        k_sleep(K_MSEC(THREAD_B_PERIOD_MS));
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * THREAD C — MQ-7 CO Gas Sensor (500ms, priority 6)
 * ═══════════════════════════════════════════════════════════════════════════ */
K_THREAD_STACK_DEFINE(stack_c, STACK_C);
static struct k_thread thread_c_data;

static void thread_c_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    while (1) {
        /* Take 8 samples, pick MAX to overcome MQ-7 heating cycle oscillation */
        float max_v = 0.0f;
        for (int s = 0; s < 8; s++) {
            float pin_v = read_adc_channel(CH_GAS);
            float recon = pin_v * MQ7_DIVIDER_R; /* Restore 0-5V from divider */
            if (recon > max_v) max_v = recon;
            k_sleep(K_MSEC(5));
        }

        float co_ppm = 0.0f;
        if (max_v >= MQ7_DEADBAND) {
            float Rs    = MQ7_RL * (MQ7_VCC - max_v) / max_v;
            float ratio = Rs / MQ7_Ro;
            co_ppm = MQ7_A * powf(ratio, MQ7_B);
            if (co_ppm < 0.0f) co_ppm = 0.0f;
        }

        k_mutex_lock(&telemetry_mutex, K_FOREVER);
        g_telem.co_ppm     = co_ppm;
        g_telem.ts_gas_ms  = k_uptime_get_32();
        k_mutex_unlock(&telemetry_mutex);

        k_sleep(K_MSEC(THREAD_C_PERIOD_MS));
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * THREAD D — Serial Monitor Print (100ms, priority 8 — LOWEST)
 * Reads snapshot from shared telemetry and prints to UART console.
 * ═══════════════════════════════════════════════════════════════════════════ */
K_THREAD_STACK_DEFINE(stack_d, STACK_D);
static struct k_thread thread_d_data;

static void thread_d_entry(void *p1, void *p2, void *p3) {
    ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

    while (1) {
        /* Take a snapshot */
        ev_telemetry_t snap;
        k_mutex_lock(&telemetry_mutex, K_FOREVER);
        memcpy(&snap, &g_telem, sizeof(ev_telemetry_t));
        k_mutex_unlock(&telemetry_mutex);

        /* Format and print — identical format to confirmed working Arduino sketch */
        printk("C1: %.3fV | C2: %.3fV | C3: %.3fV | C4: %.3fV || "
               "Amps: %.3fA || ",
               (double)snap.cell_v[0], (double)snap.cell_v[1],
               (double)snap.cell_v[2], (double)snap.cell_v[3],
               (double)snap.current_a);

        /* Temperature */
        printk("T1: ");
        if (snap.temp_c[0] <= -127.0f) printk("ERR");
        else printk("%.1fC", (double)snap.temp_c[0]);

        printk(" | T2: ");
        if (snap.temp_c[1] <= -127.0f) printk("ERR");
        else printk("%.1fC", (double)snap.temp_c[1]);

        /* CO Gas */
        printk(" || CO: %.1f ppm\n", (double)snap.co_ppm);

        k_sleep(K_MSEC(THREAD_D_PERIOD_MS));
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * MAIN — Initialize devices and launch all 4 threads
 * ═══════════════════════════════════════════════════════════════════════════ */
int main(void) {
    printk("\n================================================\n");
    printk("  EV Guardian — Zephyr RTOS Telemetry v2.0\n");
    printk("  4 Threads: Voltage(50ms) Temp(100ms)\n");
    printk("             Gas(500ms)    Print(100ms)\n");
    printk("  Calibrations: Scale=5.8159 | ACS712=0.1765V/A\n");
    printk("================================================\n\n");

    /* ── Initialize ADC ── */
    adc_dev = DEVICE_DT_GET(ADC_NODE);
    if (!device_is_ready(adc_dev)) {
        printk("ERROR: ADC device not ready!\n");
        return -1;
    }

    /* ── Initialize GPIO for DS18B20 ── */
    temp_gpio_dev = DEVICE_DT_GET(DT_NODELABEL(gpioa));
    if (!device_is_ready(temp_gpio_dev)) {
        printk("ERROR: GPIO device not ready!\n");
        return -1;
    }

    /* Configure DS18B20 pins as INPUT_PULLUP initially */
    gpio_pin_configure(temp_gpio_dev, TEMP1_PIN, GPIO_INPUT | GPIO_PULL_UP);
    gpio_pin_configure(temp_gpio_dev, TEMP2_PIN, GPIO_INPUT | GPIO_PULL_UP);

    /* ── Initialize mutex ── */
    k_mutex_init(&telemetry_mutex);

    /* ── Launch Thread A — Voltage & Current (priority 2, highest) ── */
    k_thread_create(&thread_a_data, stack_a, K_THREAD_STACK_SIZEOF(stack_a),
                    thread_a_entry, NULL, NULL, NULL,
                    2, 0, K_NO_WAIT);
    k_thread_name_set(&thread_a_data, "volt_curr");

    /* ── Launch Thread B — Temperature (priority 4) ── */
    k_thread_create(&thread_b_data, stack_b, K_THREAD_STACK_SIZEOF(stack_b),
                    thread_b_entry, NULL, NULL, NULL,
                    4, 0, K_NO_WAIT);
    k_thread_name_set(&thread_b_data, "temperature");

    /* ── Launch Thread C — CO Gas (priority 6) ── */
    k_thread_create(&thread_c_data, stack_c, K_THREAD_STACK_SIZEOF(stack_c),
                    thread_c_entry, NULL, NULL, NULL,
                    6, 0, K_NO_WAIT);
    k_thread_name_set(&thread_c_data, "co_gas");

    /* ── Launch Thread D — Serial Print (priority 8, lowest) ── */
    k_thread_create(&thread_d_data, stack_d, K_THREAD_STACK_SIZEOF(stack_d),
                    thread_d_entry, NULL, NULL, NULL,
                    8, 0, K_NO_WAIT);
    k_thread_name_set(&thread_d_data, "serial_print");

    printk("All 4 threads launched. Telemetry running...\n\n");
    return 0;
}
