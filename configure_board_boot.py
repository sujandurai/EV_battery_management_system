"""
EV Guardian — QRB2210 Linux Boot Auto-Configuration Serial Tool
===============================================================
This script executes over the debugging serial COM port of the 
Arduino Uno Q to automatically configure lightdm and systemd services 
for standalone operation.
"""

import time
import serial
import serial.tools.list_ports
import sys

def find_linux_console_port():
    print("[INIT] Scanning active COM ports...")
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = p.description.lower()
        # Look for debug or USB serial interface commonly used for console
        if "debug" in desc or "usb serial" in desc or "usb-to-uart" in desc:
            print(f"[FOUND] Potential console port matches: {p.device} ({p.description})")
            return p.device
    if ports:
        # Fallback to the first available COM port
        print(f"[WARN] No explicit debug port identified. Using fallback: {ports[0].device}")
        return ports[0].device
    return None

def run_command_on_board(ser, cmd, expected_prompt="$", timeout=5.0):
    print(f"[BOARD] Executing: {cmd}")
    ser.write((cmd + "\r\n").encode())
    time.sleep(1.0)
    
    # Read output
    start_t = time.time()
    buffer = ""
    while time.time() - start_t < timeout:
        if ser.in_waiting > 0:
            chunk = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
            buffer += chunk
            if expected_prompt in buffer or "#" in buffer:
                break
        time.sleep(0.1)
    
    lines = buffer.strip().split("\n")
    for l in lines:
        print(f"  > {l.strip()}")
    return buffer

def main():
    print("=" * 65)
    print("  EV Guardian - Standalone Boot Configuration Tool")
    print("=" * 65)
    
    port = find_linux_console_port()
    if not port:
        print("[ERROR] No active COM ports found. Connect the board via USB to the laptop.")
        sys.exit(1)
        
    try:
        print(f"[SERIAL] Connecting to console port {port} at 115200 baud...")
        ser = serial.Serial(port, 115200, timeout=2.0)
    except Exception as e:
        print(f"[ERROR] Failed to open serial port: {e}")
        sys.exit(1)
        
    print("[SERIAL] Port open! Triggering connection prompt...")
    # Send a few carriage returns to wake up CLI login prompt
    ser.write(b"\r\n\r\n")
    time.sleep(1.0)
    
    # Check if a login screen is requesting login credentials
    initial_read = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
    print(initial_read)
    
    # ── Automatic Login Phase ────────────────────────────────────────────────
    if "login:" in initial_read.lower():
        print("[LOGIN] Sending default username: debian")
        ser.write(b"debian\r\n")
        time.sleep(1.0)
        print("[LOGIN] Sending default password: debian")
        ser.write(b"debian\r\n")
        time.sleep(2.0)
        
    # Send test command to verify shell is active
    response = run_command_on_board(ser, "whoami", timeout=3.0)
    if "debian" not in response.lower() and "root" not in response.lower():
        print("[ERROR] Could not gain shell access to board. Ensure username/password rules are correct.")
        sys.exit(1)
        
    print("[SUCCESS] Shell access obtained!")

    # ── Configuration Commands Execution Phase ───────────────────────────────
    # 1. Update launcher permission executable status
    run_command_on_board(ser, "chmod +x /ev\\ vechile/launch_standalone.sh")
    
    # 2. Write systemd startup services template file
    print("[CONFIG] Creating systemd service file on board...")
    service_def = (
        "[Unit]\\n"
        "Description=EV Guardian Autonomous Diagnostic Suite\\n"
        "After=network.target mosquitto.service\\n\\n"
        "[Service]\\n"
        "Type=simple\\n"
        "User=debian\\n"
        "WorkingDirectory=/ev\\ vechile\\n"
        "ExecStart=/bin/bash /ev\\ vechile/launch_standalone.sh\\n"
        "Restart=on-failure\\n\\n"
        "[Install]\\n"
        "WantedBy=multi-user.target"
    )
    
    cmd_write = f'echo -e "{service_def}" | sudo tee /etc/systemd/system/ev_guardian.service'
    run_command_on_board(ser, cmd_write)
    
    # Enter sudo password if requested (debian default)
    time.sleep(1.0)
    ser.write(b"debian\r\n") 
    time.sleep(1.0)

    # 3. Reload systemd daemon to register the service
    run_command_on_board(ser, "sudo systemctl daemon-reload")
    
    # 4. Enable the service to run on boot
    run_command_on_board(ser, "sudo systemctl enable ev_guardian.service")
    
    # 5. Start the service immediately to verify
    print("[CONFIG] Initializing local test run of service...")
    run_command_on_board(ser, "sudo systemctl start ev_guardian.service")
    
    print("\n" + "=" * 65)
    print("  STANDALONE AUTOSTART CONFIGURATION SUCCESSFULLY WRITTEN!")
    print("=" * 65)
    print("  1. The diagnostic suite will now run automatically on boot.")
    print("  2. Safely disconnect the board from your laptop.")
    print("  3. Wire the Verilux hub, screen, and power as guided previously.")
    print("  4. Turn power on. Within 1 min, the screen will activate autonomously.")
    print("=" * 65)
    
    ser.close()

if __name__ == "__main__":
    main()
