"""
EV Guardian — QRB2210 MPU Gateway Daemon (Step: Gateway Layer)
===============================================================
Runs on the Qualcomm QRB2210 Dragonwing MPU (Debian Linux).
Bridges the STM32 IPC shared memory region to the MQTT network.

Loop A (this file): Reads telemetry from IPC → publishes to MQTT
Loop B (this file): Subscribes to trust_status → writes back to IPC
Store-and-Forward  : Caches to disk when MQTT is offline
BLE Beacon         : Broadcasts safety state via BlueZ GATT (stub)

In production: IPC bridge is via Qualcomm hardware interconnect driver.
In simulation : Reads from shared memory file (mmap) for desktop testing.
"""

import time
import json
import logging
import os
import threading
import argparse
import struct
import mmap
import ctypes
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("qrb2210_gateway")

# ── Configuration ─────────────────────────────────────────────────────────────
MQTT_BROKER       = "localhost"      # Snapdragon X PC IP in production
MQTT_PORT         = 1883
TOPIC_TELEMETRY   = "ev/sensor/telemetry"
TOPIC_TRUST       = "ev/analytics/trust_status"
LOOP_PERIOD_SEC   = 0.1             # 10 Hz (mirrors STM32 Thread A)
CACHE_FILE        = "/tmp/ev_telemetry_cache.jsonl"
DEVICE_ID         = "ev-uno-q-01"

# ── IPC Shared Memory Layout (matches firmware/main.c ipc_telemetry_t) ───────
# struct ipc_telemetry_t {
#   uint32_t timestamp_ms;      // 4
#   float    cell_v[4];         // 16
#   float    temp_c[4];         // 16
#   float    current_a;         // 4
#   float    vibration_g;       // 4
#   float    gas_ppm;           // 4
#   uint8_t  fault_flags;       // 1
#   uint8_t  node_status;       // 1
#   uint16_t checksum;          // 2
# }  total = 52 bytes

IPC_STRUCT_FMT  = "<I 4f 4f f f f B B H"  # little-endian
IPC_STRUCT_SIZE = struct.calcsize(IPC_STRUCT_FMT)  # 52 bytes

IPC_TRUST_FMT   = "<I"
TRUST_STATUS_OK    = 0x00000001
TRUST_STATUS_FAULT = 0x000000FF

# Simulated IPC file (production: /dev/mem or Qualcomm interconnect device)
IPC_SIM_FILE    = "/tmp/ev_ipc_sram.bin"
IPC_TELEMETRY_OFFSET = 0x000
IPC_TRUST_OFFSET     = 0x100
IPC_TOTAL_SIZE       = 0x200

# ── IPC Bridge (Simulation layer for desktop) ─────────────────────────────────
class IPCBridge:
    """
    Simulates the QRB2210 ↔ STM32 shared SRAM interconnect.
    On real hardware: use /dev/mem or Qualcomm SLIM bus driver.
    """
    def __init__(self, ipc_file: str):
        self.ipc_file = ipc_file
        self._init_ipc_file()
        self._mmap = None

    def _init_ipc_file(self):
        if not os.path.exists(self.ipc_file):
            # Create a blank IPC region (simulates SRAM power-on state)
            with open(self.ipc_file, 'wb') as f:
                f.write(b'\x00' * IPC_TOTAL_SIZE)
            log.info(f"IPC SRAM file created: {self.ipc_file} ({IPC_TOTAL_SIZE} bytes)")

    def open(self):
        self._fd = open(self.ipc_file, 'r+b')
        self._mmap = mmap.mmap(self._fd.fileno(), IPC_TOTAL_SIZE)
        log.info(f"IPC bridge mapped: {self.ipc_file}")

    def get_telemetry(self) -> dict | None:
        """Read telemetry block from shared SRAM."""
        try:
            self._mmap.seek(IPC_TELEMETRY_OFFSET)
            raw = self._mmap.read(IPC_STRUCT_SIZE)
            fields = struct.unpack(IPC_STRUCT_FMT, raw)
            (ts, c1, c2, c3, c4, t1, t2, t3, t4,
             curr, vib, gas, faults, status, checksum) = fields
            return {
                "timestamp_ms": ts,
                "cell_v":      [c1, c2, c3, c4],
                "temp_c":      [t1, t2, t3, t4],
                "current_a":   curr,
                "vibration_g": vib,
                "gas_ppm":     gas,
                "fault_flags": faults,
                "node_status": status,
            }
        except Exception as e:
            log.error(f"IPC read error: {e}")
            return None

    def put_trust_status(self, status: int):
        """Write trust status flag back to STM32 display thread."""
        try:
            self._mmap.seek(IPC_TRUST_OFFSET)
            self._mmap.write(struct.pack(IPC_TRUST_FMT, status))
            self._mmap.flush()
        except Exception as e:
            log.error(f"IPC write error: {e}")

    def close(self):
        if self._mmap: self._mmap.close()
        if hasattr(self, '_fd'): self._fd.close()


# ── Store-and-Forward Cache ───────────────────────────────────────────────────
class OfflineCache:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def store(self, payload: dict):
        with self._lock:
            with open(self.path, 'a') as f:
                f.write(json.dumps(payload) + '\n')

    def flush(self, mqtt_client) -> int:
        """Upload cached records when back online. Returns count flushed."""
        if not os.path.exists(self.path): return 0
        flushed = 0
        remaining = []
        with self._lock:
            with open(self.path, 'r') as f:
                lines = f.readlines()
        for line in lines:
            try:
                result = mqtt_client.publish(TOPIC_TELEMETRY, line.strip())
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    flushed += 1
                else:
                    remaining.append(line)
            except Exception:
                remaining.append(line)
        with self._lock:
            with open(self.path, 'w') as f:
                f.writelines(remaining)
        if flushed > 0:
            log.info(f"[CACHE] Flushed {flushed} offline records to MQTT")
        return flushed

    def size(self) -> int:
        if not os.path.exists(self.path): return 0
        with open(self.path, 'r') as f:
            return sum(1 for _ in f)


# ── BLE Emergency Broadcast Stub ──────────────────────────────────────────────
class BLEBeacon:
    """
    In production: Uses BlueZ D-Bus API to write GATT characteristic.
    Stub for desktop simulation.
    """
    def update(self, is_fault: bool, payload: dict):
        status_str = "FAULT" if is_fault else "OK"
        # Production: gdbus call to /org/bluez/hci0 GATT characteristic
        # log.debug(f"[BLE] GATT characteristic updated: {status_str}")
        pass


# ── MQTT Gateway ──────────────────────────────────────────────────────────────
class QRB2210Gateway:
    def __init__(self, ipc_bridge: IPCBridge, mode: str = "sim"):
        self.bridge   = ipc_bridge
        self.cache    = OfflineCache(CACHE_FILE)
        self.ble      = BLEBeacon()
        self.mode     = mode
        self._connected = False
        self._running   = False

        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "qrb2210_gateway")
        self.mqtt_client.on_connect    = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message    = self._on_trust_message

        self._loop_count   = 0
        self._online_count = 0
        self._cached_count = 0

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            self._connected = True
            client.subscribe(TOPIC_TRUST)
            log.info(f"[MQTT] Connected. Subscribed to '{TOPIC_TRUST}'")
        else:
            log.warning(f"[MQTT] Connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc, props=None, reason=None):
        self._connected = False
        log.warning(f"[MQTT] Disconnected rc={rc} — entering store-and-forward mode")

    def _on_trust_message(self, client, userdata, message):
        """Loop B: Receive trust status from Snapdragon X → write to STM32 IPC."""
        try:
            payload = json.loads(message.payload.decode())
            status_str = payload.get("status", "OK").upper()
            ipc_val    = TRUST_STATUS_FAULT if status_str == "FAULT" else TRUST_STATUS_OK
            self.bridge.put_trust_status(ipc_val)
            log.info(f"[LOOP-B] Trust status → STM32 IPC: {status_str} (0x{ipc_val:08X})")
        except Exception as e:
            log.error(f"[LOOP-B] Error: {e}")

    def _build_mqtt_payload(self, ipc_data: dict) -> dict:
        """Format IPC data as MQTT JSON matching dummy_publisher.py schema."""
        return {
            "timestamp": int(time.time() * 1000),
            "device_id": DEVICE_ID,
            "cells": {
                "voltage_v": [round(v, 4) for v in ipc_data["cell_v"]],
                "temp_c":    [round(t, 2) for t in ipc_data["temp_c"]],
            },
            "pack": {
                "current_a":   round(ipc_data["current_a"],   3),
                "vibration_g": round(ipc_data["vibration_g"], 4),
                "gas_ppm":     round(ipc_data["gas_ppm"],     1),
            },
            "metadata": {
                "node_status":  "OK" if ipc_data["node_status"] == 0 else "SENSOR_FAULT",
                "fault_flags":  ipc_data["fault_flags"],
                "ipc_ts_ms":    ipc_data["timestamp_ms"],
            }
        }

    def loop_a(self):
        """Loop A: Pull telemetry from IPC → publish to MQTT at 10Hz."""
        log.info(f"[LOOP-A] Telemetry gateway started ({1/LOOP_PERIOD_SEC:.0f}Hz)")

        while self._running:
            t_start = time.monotonic()
            self._loop_count += 1

            # In simulation mode: use dummy data (mirrors dummy_publisher.py)
            if self.mode == "sim":
                ipc_data = self._get_sim_data()
            else:
                ipc_data = self.bridge.get_telemetry()

            if ipc_data is None:
                time.sleep(LOOP_PERIOD_SEC)
                continue

            payload   = self._build_mqtt_payload(ipc_data)
            json_str  = json.dumps(payload)
            is_fault  = ipc_data["fault_flags"] != 0

            if self._connected:
                result = self.mqtt_client.publish(TOPIC_TELEMETRY, json_str)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    self._online_count += 1
                    # Flush any cached records now that we're online
                    self.cache.flush(self.mqtt_client)
                    self.ble.update(is_fault, payload)
                else:
                    self.cache.store(payload)
                    self._cached_count += 1
            else:
                # Store-and-forward: cache to disk
                self.cache.store(payload)
                self._cached_count += 1
                self.ble.update(is_fault, payload)

            # Status line every 50 cycles
            if self._loop_count % 50 == 0:
                mode_str  = "ONLINE" if self._connected else "OFFLINE(store-fwd)"
                cache_sz  = self.cache.size()
                v = ipc_data["cell_v"]
                log.info(f"[LOOP-A] #{self._loop_count:05d} {mode_str} | "
                         f"V=[{round(v[0],2)},{round(v[1],2)},{round(v[2],2)},{round(v[3],2)}] | "
                         f"Online={self._online_count} Cached={cache_sz}")

            elapsed  = time.monotonic() - t_start
            sleep_t  = max(0, LOOP_PERIOD_SEC - elapsed)
            time.sleep(sleep_t)

    # ── Simulation data generator (no physical hardware) ──────────────────
    _sim_fault = False
    _sim_cycle = 0

    def _get_sim_data(self) -> dict:
        import random
        self._sim_cycle += 1
        self._sim_fault = (self._sim_cycle // 100) % 2 == 1  # flip every 10s
        if self._sim_fault:
            v = [3.82, 3.81, 1.25, 3.80]
            t = [34.2, 34.0, 115.2, 34.1]
            curr = -12.5; vib = 0.08; gas = 12.0; faults = 0x03
        else:
            v = [round(r + random.uniform(-0.02, 0.02), 3) for r in [3.82, 3.80, 3.79, 3.81]]
            t = [round(34.0 + random.uniform(0, 0.5), 1)] * 4
            curr = round(-1.5 + random.uniform(-0.5, 0.5), 2)
            vib  = round(0.03 + random.uniform(0, 0.02), 3)
            gas  = round(5.0  + random.uniform(0, 3), 1)
            faults = 0x00
        return {
            "timestamp_ms": int(time.time() * 1000),
            "cell_v": v, "temp_c": t,
            "current_a": curr, "vibration_g": vib, "gas_ppm": gas,
            "fault_flags": faults, "node_status": 0 if faults == 0 else 1
        }
    # ──────────────────────────────────────────────────────────────────────

    def start(self):
        self._running = True

        # Connect MQTT (non-blocking)
        log.info(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
        try:
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            log.warning(f"[MQTT] Cannot connect: {e} — starting in offline mode")

        # Run Loop A in this thread
        try:
            self.loop_a()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        self._running = False
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()
        log.info(f"[STOP] Gateway stopped. Online={self._online_count} "
                 f"Cached={self._cached_count} Total={self._loop_count}")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Guardian QRB2210 Gateway Daemon")
    parser.add_argument("--mode", choices=["sim", "hw"], default="sim",
                        help="'sim' = software simulator, 'hw' = real IPC hardware")
    parser.add_argument("--broker", default=MQTT_BROKER,
                        help="MQTT broker IP (default: localhost)")
    args = parser.parse_args()

    MQTT_BROKER = args.broker

    print("=" * 65)
    print("  EV Guardian — QRB2210 MPU Gateway Daemon")
    print("=" * 65)
    print(f"  Mode      : {'SIMULATION' if args.mode == 'sim' else 'HARDWARE IPC'}")
    print(f"  MQTT      : {MQTT_BROKER}:{MQTT_PORT}")
    print(f"  Topic OUT : {TOPIC_TELEMETRY}")
    print(f"  Topic IN  : {TOPIC_TRUST}")
    print(f"  Cache     : {CACHE_FILE}")
    print(f"  Rate      : {1/LOOP_PERIOD_SEC:.0f} Hz\n")

    bridge  = IPCBridge(IPC_SIM_FILE)
    if args.mode == "hw":
        bridge.open()

    gateway = QRB2210Gateway(bridge, mode=args.mode)

    try:
        gateway.start()
    finally:
        if args.mode == "hw":
            bridge.close()
