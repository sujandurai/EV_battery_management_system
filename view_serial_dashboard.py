import sys
import os
import json
import re
import tkinter as tk
from tkinter import ttk
import serial
import serial.tools.list_ports
import threading
import time

class SerialDashboardApp(tk.Tk):
    def __init__(self, port_name):
        super().__init__()
        self.port_name = port_name
        self.title("EV Guardian - Realtime telemetry HUD")
        
        # Dark Theme Palette
        self.bg_color = "#0B0F19"
        self.card_bg = "#151D30"
        self.text_color = "#E2E8F0"
        self.accent_color = "#3B82F6"
        self.alert_danger = "#EF4444"
        self.alert_warning = "#F59E0B"
        self.alert_success = "#10B981"
        
        self.configure(bg=self.bg_color)
        
        # Target the second display monitor if it exists
        self.locate_second_monitor()
        
        # Data bindings
        self.cell_voltages = ["0.000V", "0.000V", "0.000V", "0.000V"]
        self.pack_current = "0.000A"
        self.temp1 = "0.0 C"
        self.temp2 = "0.0 C"
        self.co_gas = "0.0 ppm"
        self.mpu_status = "OK"
        
        self.ml_trust = "N/A"
        self.ml_severity = "UNKNOWN"
        self.ml_anomalies = []
        self.ml_recommendation = "Awaiting first reading..."

        self.setup_ui()
        
        # Start serial reader thread
        self.running = True
        self.json_accumulator = ""
        self.inside_json = False
        self.brace_count = 0
        
        self.serial_thread = threading.Thread(target=self.read_serial_loop, daemon=True)
        self.serial_thread.start()

    def locate_second_monitor(self):
        # Force windows geometry to position on extended screen if coordinate > 1366
        # Checking window screen width to make it cover extended area
        self.update_idletasks()
        try:
            # Let User toggle full screen easily with F11
            self.bind("<F11>", lambda event: self.attributes("-fullscreen", not self.attributes("-fullscreen")))
            self.bind("<Escape>", lambda event: self.attributes("-fullscreen", False))
            
            # Default geometry: Launch big window visible on main screen
            self.geometry("1024x600+100+100")
        except Exception:
            self.geometry("1024x600")

    def setup_ui(self):
        # Main Layout Rows/Cols
        self.grid_columnconfigure(0, weight=2) # Serial terminal logger
        self.grid_columnconfigure(1, weight=3) # Gauges & Cards
        self.grid_rowconfigure(0, weight=1)
        
        # ── LEFT PANEL: RAW SERIAL OUTPUT ────────────────────────────────────
        left_frame = tk.Frame(self, bg=self.bg_color, padx=10, pady=10)
        left_frame.grid(row=0, column=0, sticky="nsew")
        left_frame.grid_rowconfigure(1, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)
        
        lbl_log = tk.Label(left_frame, text="RAW HARDWARE SERIAL LOG", font=("Segoe UI", 11, "bold"), fg="#94A3B8", bg=self.bg_color)
        lbl_log.grid(row=0, column=0, sticky="w", pady=(0, 5))
        
        self.txt_log = tk.Text(left_frame, bg="#030712", fg="#34D399", font=("Consolas", 10), bd=0, highlightthickness=1, highlightbackground="#1E293B")
        self.txt_log.grid(row=1, column=0, sticky="nsew")
        
        # ── RIGHT PANEL: GRAPHICAL HUD ───────────────────────────────────────
        right_frame = tk.Frame(self, bg=self.bg_color, padx=10, pady=10)
        right_frame.grid(row=0, column=1, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_columnconfigure(1, weight=1)
        right_frame.grid_rowconfigure(0, weight=1) # Cells & Sensors Card
        right_frame.grid_rowconfigure(1, weight=1) # Machine Learning Diagnostics Card
        
        # 1. Sensors Card
        sensor_card = tk.Frame(right_frame, bg=self.card_bg, padx=15, pady=15, highlightthickness=1, highlightbackground="#1E293B")
        sensor_card.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 10))
        sensor_card.grid_columnconfigure((0, 1, 2, 3), weight=1)
        
        tk.Label(sensor_card, text="LIVE SENSOR METRICS", font=("Segoe UI", 12, "bold"), fg=self.accent_color, bg=self.card_bg).grid(row=0, column=0, columnspan=4, sticky="w")
        
        # Cell Voltages Grid
        self.lbl_cells = []
        for i in range(4):
            f = tk.Frame(sensor_card, bg="#1E293B", pady=5)
            f.grid(row=1, column=i, padx=5, pady=10, sticky="nsew")
            tk.Label(f, text=f"CELL {i+1}", font=("Segoe UI", 9), fg="#94A3B8", bg="#1E293B").pack()
            l = tk.Label(f, text="0.000V", font=("Segoe UI", 14, "bold"), fg=self.text_color, bg="#1E293B")
            l.pack()
            self.lbl_cells.append(l)
            
        # Other Sensors
        other_frame = tk.Frame(sensor_card, bg=self.card_bg)
        other_frame.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=(10, 0))
        other_frame.grid_columnconfigure((0, 1, 2), weight=1)
        
        # Current
        f_curr = tk.Frame(other_frame, bg=self.card_bg)
        f_curr.grid(row=0, column=0, sticky="nsew")
        tk.Label(f_curr, text="Pack Current", font=("Segoe UI", 10), fg="#94A3B8", bg=self.card_bg).pack(anchor="w")
        self.lbl_curr = tk.Label(f_curr, text="0.000 A", font=("Segoe UI", 16, "bold"), fg="#38BDF8", bg=self.card_bg)
        self.lbl_curr.pack(anchor="w")
        
        # Temps
        f_temp = tk.Frame(other_frame, bg=self.card_bg)
        f_temp.grid(row=0, column=1, sticky="nsew")
        tk.Label(f_temp, text="Temps (T1 / T2)", font=("Segoe UI", 10), fg="#94A3B8", bg=self.card_bg).pack(anchor="w")
        self.lbl_temp = tk.Label(f_temp, text="0.0°C / ERR", font=("Segoe UI", 16, "bold"), fg="#FB923C", bg=self.card_bg)
        self.lbl_temp.pack(anchor="w")
        
        # Gas & MPU
        f_gas = tk.Frame(other_frame, bg=self.card_bg)
        f_gas.grid(row=0, column=2, sticky="nsew")
        tk.Label(f_gas, text="CO Gas / IMU State", font=("Segoe UI", 10), fg="#94A3B8", bg=self.card_bg).pack(anchor="w")
        self.lbl_gas = tk.Label(f_gas, text="0.0 ppm (OK)", font=("Segoe UI", 16, "bold"), fg="#A78BFA", bg=self.card_bg)
        self.lbl_gas.pack(anchor="w")
        
        # 2. Diagnostics ML Card
        ml_card = tk.Frame(right_frame, bg=self.card_bg, padx=15, pady=15, highlightthickness=1, highlightbackground="#1E293B")
        ml_card.grid(row=1, column=0, columnspan=2, sticky="nsew")
        
        tk.Label(ml_card, text="AI / ML DIAGNOSIS PORTAL", font=("Segoe UI", 12, "bold"), fg="#10B981", bg=self.card_bg).pack(anchor="w")
        
        # Severity Banner
        self.lbl_severity = tk.Label(ml_card, text="SYSTEM STATUS: PENDING", font=("Segoe UI", 14, "bold"), bg="#1E293B", fg="#94A3B8", pady=8)
        self.lbl_severity.pack(fill="x", pady=10)
        
        # Trust Score & Anomalies
        stats_frame = tk.Frame(ml_card, bg=self.card_bg)
        stats_frame.pack(fill="x", pady=5)
        stats_frame.grid_columnconfigure((0, 1), weight=1)
        
        # Left stats
        f_st_l = tk.Frame(stats_frame, bg=self.card_bg)
        f_st_l.grid(row=0, column=0, sticky="nsew")
        tk.Label(f_st_l, text="Diagnostic Trust Level", font=("Segoe UI", 10), fg="#94A3B8", bg=self.card_bg).pack(anchor="w")
        self.lbl_trust = tk.Label(f_st_l, text="--%", font=("Segoe UI", 24, "bold"), fg="#10B981", bg=self.card_bg)
        self.lbl_trust.pack(anchor="w")
        
        # Right stats
        f_st_r = tk.Frame(stats_frame, bg=self.card_bg)
        f_st_r.grid(row=0, column=1, sticky="nsew")
        tk.Label(f_st_r, text="Anomalous Flags", font=("Segoe UI", 10), fg="#94A3B8", bg=self.card_bg).pack(anchor="w")
        self.lbl_flags = tk.Label(f_st_r, text="None", font=("Segoe UI", 11), fg="#EF4444", bg=self.card_bg, wraplength=200, justify="left")
        self.lbl_flags.pack(anchor="w")
        
        # Recommendation Banner
        tk.Label(ml_card, text="Actionable Recommendation:", font=("Segoe UI", 9, "bold"), fg="#94A3B8", bg=self.card_bg).pack(anchor="w", pady=(10, 2))
        self.lbl_rec = tk.Label(ml_card, text="Waiting for diagnostic stream...", font=("Segoe UI", 10, "italic"), fg="#E2E8F0", bg=self.card_bg, wraplength=450, justify="left")
        self.lbl_rec.pack(anchor="w")

    def append_log(self, text):
        self.txt_log.insert(tk.END, text + "\n")
        self.txt_log.see(tk.END)
        # Limit text lines to 100 to avoid memory overflow
        if float(self.txt_log.index('end-1c')) > 100:
            self.txt_log.delete('1.0', '2.0')

    def parse_stm32_line(self, line):
        # Run parsing regex
        # C1: 13.648V | C2: 3.879V | C3: 1.174V | C4: 0.248V || CurrRaw: 16383 | CurrPin: 6.600V | Amps: 23.854A || T1: -0.1C | T2: ERR || CO: 20.3 ppm || MPU: ERR
        try:
            if "C1:" in line:
                c1 = re.search(r"C1:\s*([\d\.\-]+)V", line)
                c2 = re.search(r"C2:\s*([\d\.\-]+)V", line)
                c3 = re.search(r"C3:\s*([\d\.\-]+)V", line)
                c4 = re.search(r"C4:\s*([\d\.\-]+)V", line)
                amps = re.search(r"Amps:\s*([\d\.\-]+)A", line)
                t1 = re.search(r"T1:\s*([\d\.\-]+)C", line)
                t2 = re.search(r"T2:\s*(ERR|[\d\.\-]+)", line)
                co = re.search(r"CO:\s*([\d\.\-]+)\s*ppm", line)
                mpu = re.search(r"MPU:\s*([A-Za-z\s\(\)]+)", line)
                
                if c1: self.cell_voltages[0] = f"{float(c1.group(1)):.3f}V"
                if c2: self.cell_voltages[1] = f"{float(c2.group(1)):.3f}V"
                if c3: self.cell_voltages[2] = f"{float(c3.group(1)):.3f}V"
                if c4: self.cell_voltages[3] = f"{float(c4.group(1)):.3f}V"
                if amps: self.pack_current = f"{float(amps.group(1)):.2f} A"
                
                # Format Temperature text
                t1_txt = f"{t1.group(1)}°C" if t1 else "ERR"
                t2_txt = t2.group(1) if t2 else "ERR"
                if t2_txt != "ERR":
                    t2_txt = f"{float(t2_txt):.1f}°C"
                self.temp1 = t1_txt
                self.temp2 = t2_txt
                
                if co: self.co_gas = f"{float(co.group(1)):.1f} ppm"
                if mpu: self.mpu_status = mpu.group(1).split("(")[0].strip()
                
                # Update UI elements
                self.after(0, self.update_sensor_ui)
        except Exception as e:
            self.append_log(f"[PARSE ERR] Line parse error: {e}")

    def update_sensor_ui(self):
        for i in range(4):
            self.lbl_cells[i].config(text=self.cell_voltages[i])
        self.lbl_curr.config(text=self.pack_current)
        self.lbl_temp.config(text=f"{self.temp1} / {self.temp2}")
        self.lbl_gas.config(text=f"{self.co_gas} ({self.mpu_status})")

    def parse_diagnostic_json(self, json_str):
        try:
            data = json.loads(json_str)
            self.ml_trust = f"{data.get('overall_trust', '--')}%"
            self.ml_severity = str(data.get('severity', 'UNKNOWN')).upper()
            self.ml_anomalies = data.get('anomalous_sensors', [])
            self.ml_recommendation = data.get('recommendation', 'No recommendation generated.')
            
            self.after(0, self.update_ml_ui)
        except Exception as e:
            self.append_log(f"[JSON ERR] Failed to parse diagnostic output: {e}")

    def update_ml_ui(self):
        self.lbl_trust.config(text=self.ml_trust)
        
        # Color coding severity
        if self.ml_severity == "CRITICAL" or self.ml_severity == "HIGH":
            self.lbl_severity.config(text=f"SYSTEM STATUS: {self.ml_severity}", bg=self.alert_danger, fg="#FFFFFF")
            self.lbl_trust.config(fg=self.alert_danger)
        elif self.ml_severity == "MEDIUM" or self.ml_severity == "WARNING":
            self.lbl_severity.config(text=f"SYSTEM STATUS: {self.ml_severity}", bg=self.alert_warning, fg="#0F172A")
            self.lbl_trust.config(fg=self.alert_warning)
        else:
            self.lbl_severity.config(text=f"SYSTEM STATUS: HEALTHY", bg=self.alert_success, fg="#FFFFFF")
            self.lbl_trust.config(fg=self.alert_success)
            
        # Anomalous flags
        if self.ml_anomalies:
            flags_txt = ", ".join(self.ml_anomalies)
        else:
            flags_txt = "None"
        self.lbl_flags.config(text=flags_txt)
        
        self.lbl_rec.config(text=self.ml_recommendation)

    def read_serial_loop(self):
        try:
            self.append_log(f"[SERIAL] Listening on {self.port_name} at 115200 baud...")
            ser = serial.Serial(self.port_name, 115200, timeout=1.0)
            
            while self.running:
                if ser.in_waiting > 0:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                        
                    self.after(0, lambda l=line: self.append_log(l))
                    
                    # Check JSON block boundaries with brace counting (handles nested JSON)
                    if not self.inside_json:
                        if "{" in line:
                            self.inside_json = True
                            self.json_accumulator = line
                            self.brace_count = line.count("{") - line.count("}")
                            if self.brace_count == 0:
                                self.inside_json = False
                                self.parse_diagnostic_json(self.json_accumulator)
                        else:
                            # Otherwise parse regular text telemetry stats
                            self.parse_stm32_line(line)
                    else:
                        self.json_accumulator += "\n" + line
                        self.brace_count += line.count("{") - line.count("}")
                        if self.brace_count <= 0:
                            self.inside_json = False
                            self.parse_diagnostic_json(self.json_accumulator)
                else:
                    time.sleep(0.02)
        except Exception as e:
            err_str = str(e)
            self.after(0, lambda err=err_str: self.append_log(f"[FATAL CONTRACT] {err}"))
            
    def destroy(self):
        self.running = False
        super().destroy()

if __name__ == "__main__":
    # Use COM34 as default for this setup
    app = SerialDashboardApp("COM34")
    app.mainloop()
