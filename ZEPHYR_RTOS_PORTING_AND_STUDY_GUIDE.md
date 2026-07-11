# Zephyr RTOS — Board Porting & Embedded Developer Study Guide

This document functions as a comprehensive, step-by-step tutorial on how to install, configure, build, and flash **Zephyr RTOS** on the **Arduino Uno Q (STM32U585)** board, alongside the core theoretical concepts you need to master to write manual RTOS code from scratch.

---

## Part 1: How to Port & Flash Zephyr on the Arduino Uno Q

To run Zephyr on the Arduino Uno Q, you must initialize the Zephyr development framework on your workstation and create a custom board definition.

### Step 1: Install the Toolchain & Environment
Install the compile-time dependencies on your operating system (command examples for Windows/PowerShell):

1. **Install Chocolatey** (Windows package manager) and then install dependencies:
   ```powershell
   choco install cmake ninja gperf python git dtc-msys2
   ```
2. **Install West** (Zephyr's project management tool):
   ```powershell
   pip3 install west
   ```
3. **Initialize the Zephyr Workspace**:
   ```powershell
   west init zephyrproject
   cd zephyrproject
   west update
   west zephyr-export
   pip3 install -r zephyr\scripts\requirements.txt
   ```
4. **Install the Zephyr SDK** (Contains compilers like `arm-none-eabi-gcc`):
   * Download the official SDK installer from [github.com/zephyrproject-rtos/sdk-ng](https://github.com/zephyrproject-rtos/sdk-ng/releases) and extract it to `C:\zephyr-sdk`.

---

### Step 2: Create the Board Configuration Directory
To support a new board, you must declare it in `zephyr/boards/arm/arduino_uno_q/`. Create the following files inside this folder:

#### 1. `Kconfig.board`
Tells the build system that this board exists and runs on the STM32U585xx SoC:
```kconfig
config BOARD_ARDUINO_UNO_Q
	bool "Arduino Uno Q"
	depends on SOC_STM32U585XX
```

#### 2. `arduino_uno_q_defconfig`
Declares default hardware clock configurations for kernel initialization:
```ini
CONFIG_BOARD_ARDUINO_UNO_Q=y
CONFIG_SOC_SERIES_STM32U5X=y
CONFIG_SOC_STM32U585XX=y
# Enable HSI/MSIS clocks configuration
CONFIG_CLOCK_CONTROL=y
CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC=160000000
```

#### 3. `arduino_uno_q.dts` (Base Device Tree)
Links physical microchip pins to system peripherals:
```dts
/dts-v1/;
#include <st/u5/stm32u585Xx.dtsi>
#include <st/u5/stm32u585aiyxtq-pinctrl.dtsi>

/ {
	model = "Arduino Uno Q Developer Board";
	compatible = "arduino,uno-q", "st,stm32u585";

	chosen {
		zephyr,console = &usart1;
		zephyr,sram = &sram0;
		zephyr,flash = &flash0;
	};
};

&usart1 {
	status = "okay";
	current-speed = <115200>;
};
```

---

### Step 3: Build & Flash Code
Navigate to your application folder containing `main.c` and compile/flash using **West**:
```powershell
# 1. Compile the code against our newly created board configuration
west build -b arduino_uno_q .

# 2. Flash the binary to the board (Requires ST-Link or USB connection)
west flash --runner openocd
```

---

## Part 2: Core Concepts to Study Before Coding Manually

To write C code for Zephyr RTOS from scratch without relying on boilerplate scripts, you must master these six fundamental embedded core architectures:

### 1. Devicetree Bindings (Hardware Configurations)
Unlike standard C variables, physical pin routes in Zephyr are declared in text overlay files (`.overlay`) and resolved at compile time.
* **Nodes and Properties**: Everything is represented as a tree node (e.g. `&i2c1` or `&adc1`). Properties configure hardware states (e.g. `reg = <0x68>`).
* **Pin Control (pinctrl)**: Configure internal pull-up, pull-down, drive strength, and mapping modes for target pins.
* **Driver Retrieval**: Understand how to obtain driver handles using:
  `DEVICE_DT_GET(DT_NODELABEL(your_node_name))`
* **What to Study**: Study how device trees resolve to `#define` structures in C and the `zephyr/device.h` header APIs.

### 2. The RTOS Thread Scheduler
Zephyr runs a preemptive, priority-based multitasking scheduler.
* **Priorities**: Negative priority values represent **Cooperative Threads** (they cannot be interrupted and run until they block or sleep). Positive priority values represent **Preemptive Threads** (the scheduler can swap them out when a higher-priority task wakes up).
* **Sleep and Yielding**: Using `k_sleep()` or `k_yield()` is mandatory to release the CPU cores to let other waiting threads execute. `k_busy_wait()` holds execution without yielding.
* **What to Study**: Learn the difference between Cooperative vs. Preemptive scheduling and the kernel's stack allocation boundaries.

### 3. Mutual Exclusion & Thread Safety
When multi-threaded systems access the same memory location, hardware pins, or communication channels concurrently, they trigger **Race Conditions**.
* **Mutexes (`k_mutex`)**: Block threads from accessing shared RAM while another thread is writing to it. Supports priority inheritance.
* **Semaphores (`k_sem`)**: Used for signaling state changes (e.g., waking up a thread only after an ADC interrupt finishes reading).
* **FIFO/LIFO Queues**: Used for transferring data structures between different processing threads safely.
* **What to Study**: Learn about Priority Inversion and how Mutexes prevent data corruption in RAM.

### 4. Device Driver APIs (Standard Peripherals)
Zephyr includes unified driver APIs. You do not write low-level code directly to register addresses; instead, you call kernel functions:
* **GPIO**: `gpio_pin_configure()`, `gpio_pin_set()`, `gpio_pin_get()`.
* **I2C**: `i2c_write()`, `i2c_read()`, `i2c_burst_read()`.
* **ADC**: `adc_channel_setup()`, `adc_read()`.
* **What to Study**: Check the official [Zephyr API Documentation](https://docs.zephyrproject.org/latest/reference/index.html) under peripheral driver API wrappers.

### 5. Interrupt Service Routines (ISRs) & Offloading
Writing long operations inside an Interrupt Handler (like reading an I2C device during a Pin Interrupt) will freeze the scheduler and crash the system.
* **ISR Restraints**: Interrupts must remain extremely fast (nanoseconds).
* **Work Queues (`k_work`)**: Use ISRs to schedule a "Work Item" on the System Work Queue. This offloads the heavy computations to a low-priority thread, freeing up interrupts.
* **What to Study**: Learn the difference between Thread context and ISR context.

### 6. The Kconfig System
Zephyr is modular. To save RAM, hardware drivers are only compiled if explicitly configured in the configuration file (`prj.conf`).
* **Overriding Features**: Enable modules using `CONFIG_GPIO=y`, `CONFIG_I2C=y`, or enable floating-point features with `CONFIG_FPU=y`.
* **What to Study**: Review how Kconfig files evaluate expressions to dynamically configure compilation libraries at build time via CMake.
