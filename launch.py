"""
EV Guardian — One-Click Launcher
=================================
Starts all backend services simultaneously:
  1. Mock Cloud AI 100 server (port 9000)
  2. XPC Backend v3  (MQTT + ONNX + WebSocket:8887 + HTTP:8766)
  3. Cloud Sync watcher (syncs to cloud every 60s)

Then opens the dashboard in your default browser.

Usage:
  python launch.py
"""

import subprocess
import sys
import time
import os
import webbrowser
import threading

BASE = os.path.dirname(os.path.abspath(__file__))

def run_proc(name, cmd, cwd=BASE):
    print(f"[LAUNCH] Starting {name}...")
    return subprocess.Popen(
        [sys.executable] + cmd,
        cwd=cwd,
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
    )

def main():
    print("=" * 65)
    print("  EV Guardian — System Launcher")
    print("=" * 65)

    procs = []

    # 1. Mock Cloud server + sync watcher
    p1 = run_proc("Cloud Sync (mock server + watcher)",
                  ["cloud_sync.py", "--mock-server"])
    procs.append(p1)
    time.sleep(1.0)

    # 2. Main backend (MQTT + ONNX + WebSocket + HTTP API)
    p2 = run_proc("XPC Backend v3.0", ["backend.py"])
    procs.append(p2)
    time.sleep(2.0)

    # 3. Dummy publisher (simulated sensor)
    p3 = run_proc("Arduino Simulator (dummy_publisher)", ["dummy_publisher.py"])
    procs.append(p3)
    time.sleep(0.5)

    # 4. Open dashboard in browser
    dashboard_path = os.path.join(BASE, "dashboard", "index.html")
    print(f"\n[LAUNCH] Opening dashboard: {dashboard_path}")
    webbrowser.open(f"file:///{dashboard_path.replace(os.sep, '/')}")

    print("\n" + "=" * 65)
    print("  ALL SERVICES RUNNING")
    print("=" * 65)
    print(f"  WebSocket  : ws://localhost:8887")
    print(f"  Diag API   : http://localhost:8766/diagnose")
    print(f"  Cloud Mock : http://localhost:9000")
    print(f"  Dashboard  : dashboard/index.html")
    print(f"\n  Press Ctrl+C to stop all services.")
    print("=" * 65)

    try:
        while True:
            time.sleep(1)
            # Check if any critical process died
            for p in procs:
                if p.poll() is not None:
                    print(f"\n[WARN] A process (pid={p.pid}) has exited.")
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down all services...")
        for p in procs:
            try: p.terminate()
            except: pass
        print("[STOP] Done.")

if __name__ == "__main__":
    main()
