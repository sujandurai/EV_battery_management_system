# EV Guardian — Zephyr RTOS Board Porting & Low-Level Operations Guide

This document provides a highly technical, deep-dive examination of how **Zephyr RTOS v3.6** is compiled, configured, and executed on the **Arduino Uno Q (STM32U585 MCU)** hardware platform. It covers every low-level integration corner including hardware device trees, clock trees, microsecond-accurate bit-banging, memory layouts, and thread safety states.

---

## 1. How Zephyr RTOS was Ported to the Arduino Uno Q

The Arduino Uno Q utilizes the **STM32U585xx** microcontroller—a high-performance ARM Cortex-M33 core running up to 160 MHz with TrustZone security extension. Driving Zephyr on this platform requires a custom board definition configuration:

### A. The Devicetree Source (`.dts` & `.overlay`) Pipeline
Zephyr uses Devicetree files to represent hardware topologies. The compilation pipeline operates as follows:

```
[stm32u585xx.dtsi] (SoC Definition)
        │
        ▼ (Inherited by)
[arduino_uno_q.dts] (Board Pin Mappings)
        │
        ▼ (Modified by)
[arduino_uno_q.overlay] (Our App Overlays)
        │
        ▼ (Handled by CMake)
     [ devicetree_generated.h ] (Compiled C Header definitions)
```

1. **Base SOC Definitions (`stm32u585xx.dtsi`)**: Provided by the Zephyr kernel, this defines core memory maps, NVIC interrupt lines, register offsets for ADC, I2C, USART, and GPIO blocks.
2. **Board definitions (`arduino_uno_q.dts`)**: Maps physical microcontroller pins to the Arduino-style headers (e.g. mapping `PA0` to `A0`, `PB6` to the I2C block).
3. **Application overlay (`arduino_uno_q.overlay`)**: We use this file to configure peripherals specifically for our EV Guardian sensors:
    * Enables the `adc1` device block and configures channels 0-4 for voltages/current.
    * Sets `i2c1` to standard Fast-mode speed ($400\text{ kHz}$) and maps the clock and data pins (`PB6` & `PB7`).
    * Configures `usart1` debug uart at $115200\text{ baud}$.

### B. Clock Tree Configuration (Achieving 160 MHz)
In `arduino_uno_q.dts`, the system clock is configured using the STM32 RCC (Reset and Clock Control) registers. The controller is clocked as follows:
* **Source**: Multi-Speed Internal Clock (MSIS) or High-Speed External Crystal (HSE).
* **Multiplication**: The System Phase-Locked Loop (PLL) multiplies the clock frequency up to **160 MHz**.
* **AHB Prescaler**: Divided by 1 to keep core memory busses running at 160 MHz.
* **APB Prescalers**: Divided by 2 for APB1/APB2 registers, ensuring the ADC peripheral clocks run safely within their specified limits ($40\text{ MHz}$ max).

---

## 2. Low-Level Sensor Drivers & Bit-Level Protocols

### A. Microsecond-Accurate OneWire Driver (DS18B20 Bit-Banging)
Because Zephyr does not have a native OneWire driver for custom GPIO pins in standard builds, the firmware uses a high-performance **software-driven bit-banging protocol** written in C. 

In a multi-threaded RTOS environment, standard software loops (like `for (int i=0; i<100);`) cannot be used for timing because they depend on CPU speed and can be interrupted by the scheduler. 
Instead, we use Zephyr's accurate hardware-timer-based call:
`k_busy_wait(uint32_t usec)`
This blocks the processor on a microsecond-precise hardware cycle counter (DWT register) without yielding the thread.

#### The Code Implementation:
```c
/* Standard 1-Wire Write Slot Sequence */
static void ow_write_bit(uint8_t pin, int bit) {
    // 1. Pull bus Low to initiate start of write slot duration
    gpio_pin_configure(temp_gpio_dev, pin, GPIO_OUTPUT_LOW);
    
    if (bit) {
        // Write 1: Pull low for 6us, then release the pin back to high-impedance (pull-up)
        k_busy_wait(6);
        gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
        k_busy_wait(54); // Hold high for rest of timeslot (60us total)
    } else {
        // Write 0: Pull low for 60us complete timeslot
        k_busy_wait(60);
        gpio_pin_configure(temp_gpio_dev, pin, GPIO_INPUT | GPIO_PULL_UP);
        k_busy_wait(10); // Recovery slot spacing
    }
}
```

### B. High-Speed DMA-Free I2C Burst Read (MPU-6050)
If we were to read Accelerometer registers ($x, y, z$) individually using separate I2C calls, the raw sensor values could update mid-read, leading to corrupted coordinates. 

To solve this, we use **I2C burst reading**:
```c
uint8_t data[14];
// Register 0x3B is the start of ACCEL_XOUT_H
int ret = i2c_burst_read(i2c_dev, MPU6050_ADDR, 0x3B, data, 14);
```
This performs a single I2C transaction that automatically increments the target register address inside the sensor chip. It reads 14 bytes sequentially:
* `data[0-5]`: Accelerometer X, Y, Z coordinates.
* `data[6-7]`: Internal die temperature.
* `data[8-13]`: Gyroscope X, Y, Z coordinates.
This guarantees that all sensor axes are synchronized to the exact same temporal point.

---

## 3. RTOS Under-the-Hood: Memory & Scheduler Layouts

### A. Memory Footprint Allocation
Each thread in Zephyr must have its own dedicated stack space allocated in SRAM. If a thread exceeds its stack, it causes a **Stack Overflow Exception**, triggering a Kernel Panic.

Memory allocations in our system:
* **Thread A Stack (`STACK_A = 2048 bytes`)**: Larger stack because it calls complex math-heavy floating-point routines.
* **Thread B Stack (`STACK_B = 2048 bytes`)**: Larger stack because OneWire operations call nested time-delay sequences.
* **Thread E Stack (`STACK_E = 2048 bytes`)**: Interacts with the I2C peripheral drivers.
* **Thread C/D Stacks (`1024 bytes`)**: Simpler formatting and state checking.

Each thread has a **Thread Control Block (TCB)** in SRAM that registers metadata including:
* Current core state (Running, Ready, Suspended, or Blocked).
* CPU registers snapshot (Program Counter, Stack Pointer, link register).
* Priority index value.

### B. Priority Inversion Mitigation (How The Mutex Works)
Our threads share a central telemetry struct `g_telem` protected by `telemetry_mutex`. 

Suppose Thread D (Priority 8, low) locks `telemetry_mutex` to print data. Suddenly, Thread A (Priority 2, high) wakes up and wants to write voltage readings, requiring `telemetry_mutex`. Since the mutex is locked, Thread A is **Blocked** (suspended from running).

Normally, a medium-priority thread (Priority 4) could wake up and preempt Thread D, preventing Thread D from ever finishing and releasing the mutex. This is called **Priority Inversion** (a lower-priority thread hangs a higher-priority thread).

**Zephyr resolves this automatically using Priority Inheritance:**
1. When Thread A (Priority 2) blocks on the mutex held by Thread D (Priority 8), the kernel temporarily raises Thread D's priority to **Priority 2**.
2. Thread D can now run, finish printing, and release the mutex without being interrupted by medium-priority threads.
3. Once the mutex is released, Thread D's priority resolves back to 8, and Thread A immediately pre-empts it to write the sensors.

---

## 4. Hardware Fault Handling & Troubleshooting

If you experience issues during development, here is how the RTOS behaves and how to debug it:

### A. Kernel Panic (HardFault Handler)
When a hardware fault occurs (such as division by zero, null-pointer dereferences, or memory access outside of SRAM boundary bounds):
1. The Cortex-M33 issues a **HardFault exception**.
2. Zephyr's fault handler catches the exception and prints register states over the USART console:
   ```text
   *** KEY EXECUTION EXCEPTION ***
   Current thread: 0x200010c4 (temperature)
   xPSR: 0x61000000 r0: 0x00000000 r1: 0x200014a0
   r12: 0x00000002 lr: 0x08002a43 pc: 0x08003f44
   ```
3. To find the source of the crash, run:
   `arm-none-eabi-addr2line -e build/zephyr/zephyr.elf 0x08003f44`
   This outputs the exact line number of the C code that crashed!

### B. Stack Guard Security
In `prj.conf`, we configure:
`CONFIG_ARM_MPU=y`
This enables the **Memory Protection Unit (MPU)**. Zephyr uses the MPU to set up hardware "guard zones" at the bottom of each thread stack. If a thread overflows its stack space, the hardware triggers a memory fault immediately before any adjacent thread RAM is corrupted, preventing erratic system crashes.
