import sqlite3

DB_FILE = "ev_telemetry.db"

def main():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Check SQLite table schema
        cursor.execute("PRAGMA table_info(telemetry_logs)")
        columns = cursor.fetchall()
        
        print("="*60)
        print("DATABASE SCHEMA VERIFICATION")
        print("="*60)
        print(f"{'Col ID':<8} | {'Column Name':<15} | {'Data Type':<10}")
        print("-"*60)
        for col in columns:
            print(f"{col[0]:<8} | {col[1]:<15} | {col[2]:<10}")
            
        # Check total record count
        cursor.execute("SELECT COUNT(*) FROM telemetry_logs")
        total_records = cursor.fetchone()[0]
        print("\n" + "="*60)
        print(f"TOTAL LOGGED PACKETS IN DATABASE: {total_records}")
        print("="*60)
        
        # Fetch the latest 3 telemetry records
        cursor.execute("SELECT * FROM telemetry_logs ORDER BY id DESC LIMIT 3")
        rows = cursor.fetchall()
        
        for i, row in enumerate(rows):
            print(f"\n[Latest Record #{i+1}] (ID: {row[0]}, Received: {row[14]})")
            print(f"  Device ID: {row[2]} | Device Timestamp: {row[1]}")
            print(f"  Cell Voltages : C1={row[3]}V, C2={row[4]}V, C3={row[5]}V, C4={row[6]}V")
            print(f"  Cell temps    : C1={row[7]}°C, C2={row[8]}°C, C3={row[9]}°C, C4={row[10]}°C")
            print(f"  Pack Current  : {row[11]} A")
            print(f"  Vibration     : {row[12]} G")
            print(f"  Gas level     : {row[13]} PPM")
            
        conn.close()
    except Exception as e:
        print(f"Verification failed: {e}")

if __name__ == "__main__":
    main()
