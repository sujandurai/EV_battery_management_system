"""
EV Guardian - Cloud AI 100 Fleet Analytics Sync (Step 10)
==========================================================
Reads anomaly events from local SQLite DB, batches them,
and uploads to the Qualcomm Cloud AI 100 fleet analytics endpoint.

In production: replace CLOUD_ENDPOINT with real Cloud AI 100 REST URL.
In demo mode : a mock HTTP server is started locally to receive the data.

Run modes:
  python cloud_sync.py             -- one-shot sync
  python cloud_sync.py --watch     -- sync every 60 seconds
  python cloud_sync.py --mock-server -- start mock cloud server
"""

import sqlite3
import json
import time
import argparse
import threading
import datetime
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Configuration ─────────────────────────────────────────────────────────────
DB_FILE             = "ev_telemetry.db"
CLOUD_ENDPOINT      = "http://localhost:9000/api/v1/fleet/telemetry"   # swap for real URL
FLEET_ID            = "EV-GUARDIAN-FLEET-001"
DEVICE_ID           = "ev-uno-q-01"
SYNC_INTERVAL_SEC   = 60
BATCH_SIZE          = 50   # records per upload

# Tracks last synced DB row id
_last_synced_id = 0

# ── Mock Cloud Server (for demo) ──────────────────────────────────────────────
class MockCloudHandler(BaseHTTPRequestHandler):
    _received_batches = 0
    _received_records = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            payload = json.loads(body)
            records = payload.get("records", [])
            MockCloudHandler._received_batches += 1
            MockCloudHandler._received_records += len(records)
            anomalies = sum(1 for r in records if r.get("is_anomaly"))
            print(f"\n[CLOUD] Batch #{MockCloudHandler._received_batches} received: "
                  f"{len(records)} records  ({anomalies} anomalies)  "
                  f"Total ingested: {MockCloudHandler._received_records}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "accepted",
                "batch_id": f"BATCH-{MockCloudHandler._received_batches:04d}",
                "records_accepted": len(records),
                "anomalies_flagged": anomalies
            }).encode())
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode())

    def log_message(self, format, *args):
        pass  # suppress default access log

def start_mock_server(port=9000):
    server = HTTPServer(("0.0.0.0", port), MockCloudHandler)
    print(f"[MOCK CLOUD] Server listening on http://localhost:{port}/api/v1/fleet/telemetry")
    server.serve_forever()

# ── SQLite Reader ─────────────────────────────────────────────────────────────
def fetch_new_records(after_id: int) -> list[dict]:
    try:
        conn   = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM telemetry_logs
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
        """, (after_id, BATCH_SIZE))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB ERR] {e}")
        return []

def fetch_fleet_summary() -> dict:
    """Aggregate stats for the fleet dashboard."""
    try:
        conn   = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM telemetry_logs")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM telemetry_logs WHERE is_anomaly=1")
        anomalies = cursor.fetchone()[0]
        cursor.execute("""
            SELECT AVG(cell_1_v), AVG(cell_2_v), AVG(cell_3_v), AVG(cell_4_v),
                   AVG(cell_1_t), AVG(cell_2_t), AVG(cell_3_t), AVG(cell_4_t),
                   MIN(cell_3_v), MAX(cell_3_t)
            FROM telemetry_logs WHERE is_anomaly=0 LIMIT 1000
        """)
        r = cursor.fetchone()
        conn.close()
        return {
            "total_packets":    total,
            "total_anomalies":  anomalies,
            "anomaly_rate_pct": round(anomalies / total * 100, 2) if total else 0,
            "avg_voltages_v":   [round(r[i] or 0, 4) for i in range(4)],
            "avg_temps_c":      [round(r[i+4] or 0, 2) for i in range(4)],
            "min_cell3_volt":   round(r[8] or 0, 4),
            "max_cell3_temp":   round(r[9] or 0, 2),
        }
    except Exception as e:
        print(f"[SUMMARY ERR] {e}")
        return {}

# ── Upload Batch ──────────────────────────────────────────────────────────────
def upload_batch(records: list[dict]) -> bool:
    if not records: return True
    payload = {
        "fleet_id":   FLEET_ID,
        "device_id":  DEVICE_ID,
        "sync_time":  datetime.datetime.utcnow().isoformat() + "Z",
        "record_count": len(records),
        "records":    records,
        "summary":    fetch_fleet_summary(),
    }
    try:
        resp = requests.post(
            CLOUD_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json", "X-Fleet-ID": FLEET_ID},
            timeout=10
        )
        if resp.status_code == 200:
            result = resp.json()
            print(f"[SYNC] Uploaded {len(records)} records | "
                  f"Batch ID: {result.get('batch_id','?')} | "
                  f"Anomalies flagged: {result.get('anomalies_flagged',0)}")
            return True
        else:
            print(f"[SYNC ERR] HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.exceptions.ConnectionError:
        print(f"[SYNC] Cloud endpoint unreachable. Records will retry next cycle.")
        return False
    except Exception as e:
        print(f"[SYNC ERR] {e}")
        return False

# ── Sync Cycle ────────────────────────────────────────────────────────────────
def run_sync_cycle():
    global _last_synced_id
    records = fetch_new_records(_last_synced_id)
    if not records:
        print(f"[SYNC] No new records since id={_last_synced_id}")
        return

    if upload_batch(records):
        _last_synced_id = records[-1]["id"]
        print(f"[SYNC] Last synced id={_last_synced_id}")

def print_fleet_report():
    s = fetch_fleet_summary()
    print("\n" + "="*65)
    print("  EV GUARDIAN — FLEET ANALYTICS SUMMARY")
    print("="*65)
    print(f"  Total Packets    : {s.get('total_packets',0)}")
    print(f"  Total Anomalies  : {s.get('total_anomalies',0)}")
    print(f"  Anomaly Rate     : {s.get('anomaly_rate_pct',0)}%")
    print(f"  Avg Cell Voltages: {s.get('avg_voltages_v',[])} V")
    print(f"  Avg Cell Temps   : {s.get('avg_temps_c',[])} C")
    print(f"  Min Cell-3 Volt  : {s.get('min_cell3_volt',0)} V")
    print(f"  Max Cell-3 Temp  : {s.get('max_cell3_temp',0)} C")
    print("="*65)

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Guardian Cloud Sync")
    parser.add_argument("--watch",       action="store_true", help=f"Sync every {SYNC_INTERVAL_SEC}s continuously")
    parser.add_argument("--mock-server", action="store_true", help="Start mock Cloud AI 100 server")
    parser.add_argument("--report",      action="store_true", help="Print fleet summary report")
    args = parser.parse_args()

    print("="*65)
    print("  EV Guardian — Cloud AI 100 Fleet Analytics Sync")
    print("="*65)
    print(f"  Endpoint : {CLOUD_ENDPOINT}")
    print(f"  Fleet ID : {FLEET_ID}")
    print(f"  DB File  : {DB_FILE}\n")

    if args.report:
        print_fleet_report()
        exit(0)

    if args.mock_server:
        # Start mock server in background, then run sync loop
        t = threading.Thread(target=start_mock_server, daemon=True)
        t.start()
        time.sleep(0.5)
        args.watch = True   # auto-enable watch mode with mock server

    if args.watch:
        print(f"[WATCH] Syncing every {SYNC_INTERVAL_SEC} seconds. Ctrl+C to stop.\n")
        while True:
            try:
                run_sync_cycle()
                time.sleep(SYNC_INTERVAL_SEC)
            except KeyboardInterrupt:
                print("\n[STOP] Cloud sync stopped.")
                print_fleet_report()
                break
    else:
        run_sync_cycle()
        print_fleet_report()
