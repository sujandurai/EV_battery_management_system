# Zephyr RTOS — Machine Learning Integration and TinyML Deployment Guide

This document describes how the machine learning pipeline is integrated with the **Arduino Uno Q (STM32U585)** hardware. It covers the current architecture and shows how to run machine learning models directly on the microcontroller.

---

## 1. The Current Split-Edge Topology (Edge Co-Processing)

In the current setup, machine learning execution is split between a **Real-Time Data Node (STM32U585 MCU)** and an **Edge Processor (Qualcomm MPU / Snapdragon X PC)**. This division leverages the strengths of both platforms:

```
┌───────────────────────────────────────┐
│     Arduino Uno Q (STM32U585 MCU)     │
│   • Runs Zephyr RTOS Multi-threading  │
│   • Collects 14-bit ADC Telemetry     │
│   • Packages signals to JSON         │
└──────────────────┬────────────────────┘
                   │
                   ▼ (115200 Baud Serial / USB)
┌───────────────────────────────────────┐
│     Snapdragon X PC / Qualcomm MPU    │
│   • Ingests JSON vector inputs        │
│   • Runs Triple ONNX Models (NPU/GPU) │
│   • Classifies trust & diagnostics   │
└──────────────────┬────────────────────┘
                   │
                   ▼ (MQTT Feedback: ev/analytics/trust_status)
┌───────────────────────────────────────┐
│     Arduino Uno Q (STM32U585 MCU)     │
│   • Reads "FAULT" or "OK" status      │
│   • Restricts MOSFET Charge current   │
│   • Drives LED Matrix (O / ! / X)     │
└───────────────────────────────────────┘
```

### A. The Telemetry Ingestion Vector
Every $100\text{ ms}$, the STM32's `serial_print` thread formats a JSON packet containing the following parameters:
$$\text{Vector Input } X = [C_1, C_2, C_3, C_4, I_{\text{pack}}, T_{\max}, \text{gas}_{\text{ppm}}, \text{vib}_{\text{g}}]$$

### B. The Snapdragon X / MPU Inference Stack
The backend ingestion daemon (`backend.py`) parses the telemetry JSON and runs three ONNX models:
1. **Anomaly Detector (`anomaly_model.onnx`)**: Uses an Isolation Forest algorithm to flag out-of-bounds telemetry vectors.
2. **SOH Estimator (`soh_model.onnx`)**: Estimates capacity loss based on charge profiling.
3. **pinn Battery Twin (`pinn_battery_twin.onnx`)**: Computes electrochemical degradation metrics, including Loss of Lithium Inventory (LLI) and Loss of Active Material (LAM).

Inference utilizes hardware acceleration on the host platform via the **QNN Execution Provider** (for the Qualcomm Hexagon NPU) or **DirectML/CUDA** (for GPUs), falling back to standard CPU threads if hardware is busy.

---

## 2. Running ML Directly on the STM32 Microcontroller (TinyML)

To run inference directly on the STM32U585 processor without depending on external host CPUs, you can port the model code to run as a local **TinyML** thread using **TensorFlow Lite for Microcontrollers (TFLM)** under Zephyr.

### Step 1: Export & Quantize the Model to a C Array
Convert the trained Keras/PyTorch model into a TensorFlow Lite flatbuffer, quantize weights to 8-bit integers (`int8`) to minimize memory footprints, and export it into a C header file using the `xxd` command:

```bash
# Convert flatbuffer to C array representation
xxd -i model.tflite > model_data.h
```

This creates a static file containing the model definition:
```c
// model_data.h
unsigned char model_tflite[] = {
  0x1c, 0x00, 0x00, 0x00, 0x54, 0x46, 0x4c, 0x33, ...
};
unsigned int model_tflite_len = 18432; // Size in bytes
```

### Step 2: Configure `prj.conf` for TinyML
Add configuration variables to enable C++ runtime support, hardware floating-point operations (FPU sharing), and import the **ARM CMSIS-NN** library parameters:

```ini
# Enable C++ compiler support (Required by TensorFlow Lite Micro)
CONFIG_CPLUSPLUS=y
CONFIG_LIB_CPLUSPLUS=y

# Enable CPU/FPU Hardware Acceleration
CONFIG_FPU=y
CONFIG_FPU_SHARING=y

# Allocate Heap Memory Pool for Model Tensors
CONFIG_HEAP_MEM_POOL_SIZE=32768
```

### Step 3: Configure `CMakeLists.txt`
Incorporate the TensorFlow Lite Micro library sources and path dependencies into your compilation step:

```cmake
# Include TFLite Micro sources in CMake
add_subdirectory(tensorflow/lite/micro)
target_link_libraries(app PRIVATE tensorflow-microlite)
```

### Step 4: Spawning the Inference Thread in C (`main.c`)
Write the code to allocate memory for the model's tensors, initialize the interpreter, feed raw telemetry, invoke inference, and update the system's safety parameters:

```c
#include <zephyr/kernel.h>
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_log.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "model_data.h" // Your quantized C-array model 

#define INFERENCE_STACK_SIZE 4096
#define INFERENCE_PRIORITY 7  // Run below ADC reading but above reporting threads

// Define tensor arena memory size (RAM allocation for network weights & activations)
constexpr int kTensorArenaSize = 16 * 1024;
uint8_t tensor_arena[kTensorArenaSize];

K_THREAD_STACK_DEFINE(inference_stack, INFERENCE_STACK_SIZE);
struct k_thread inference_thread_data;

// Shared telemetry struct references
extern ev_telemetry_t g_telem;
extern struct k_mutex telemetry_mutex;

void inference_thread_entry(void *p1, void *p2, void *p3) {
    // 1. Initialise TFLite environment
    tflite::InitializeTarget();
    
    // 2. Load the model flatbuffer from memory
    const tflite::Model* model = tflite::GetModel(model_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        printk("Model schema version mismatch!\n");
        return;
    }

    // 3. Register required operations (e.g. FullyConnected layer processing)
    static tflite::MicroMutableOpResolver<3> resolver;
    resolver.AddFullyConnected();
    resolver.AddRelu();
    resolver.AddLogistic();

    // 4. Instantiate the interpreter
    static tflite::MicroInterpreter interpreter(
        model, resolver, tensor_arena, kTensorArenaSize);
    
    if (interpreter.AllocateTensors() != kTfLiteOk) {
        printk("Tensor allocation failed!\n");
        return;
    }

    // Get pointers to input and output tensors
    TfLiteTensor* input = interpreter.input(0);
    TfLiteTensor* output = interpreter.output(0);

    printk("[TinyML] TensorFlow Lite Micro initialized successfully!\n");

    while (1) {
        ev_telemetry_t snap;
        
        // Block-copy telemetry data thread-safely
        k_mutex_lock(&telemetry_mutex, K_FOREVER);
        memcpy(&snap, &g_telem, sizeof(ev_telemetry_t));
        k_mutex_unlock(&telemetry_mutex);

        // Calculate maximum cell temperature
        float max_t = snap.temp_c[0] > snap.temp_c[1] ? snap.temp_c[0] : snap.temp_c[1];

        // 5. Feed the Normalized Input Vector into the model
        // Telemetry inputs: [C1, C2, C3, C4, Current, MaxTemp, CO, Vib]
        input->data.f[0] = snap.cell_v[0];
        input->data.f[1] = snap.cell_v[1];
        input->data.f[2] = snap.cell_v[2];
        input->data.f[3] = snap.cell_v[3];
        input->data.f[4] = snap.current_a;
        input->data.f[5] = max_t;
        input->data.f[6] = snap.co_ppm;
        input->data.f[7] = snap.vibration_g;

        // 6. Invoke Model Inference
        TfLiteStatus invoke_status = interpreter.Invoke();
        if (invoke_status != kTfLiteOk) {
            printk("[TinyML] Inference execution failed!\n");
        } else {
            // 7. Extract the model classification output
            // Output node yields threat probability (0.0 to 1.0)
            float risk_probability = output->data.f[0];

            // 8. Act on threat evaluations
            if (risk_probability > 0.85f) {
                // High risk detected: trigger fast LED fault display & limit charger current
                printk("[TinyML Warning] Battery anomaly risk level: %.2f%%\n", risk_probability * 100);
            }
        }

        // Run inference at a rate of 5 Hz (every 200 ms)
        k_sleep(K_MSEC(200));
    }
}
