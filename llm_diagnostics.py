"""
EV Guardian - LLM Diagnostics Copilot (Step 9)
================================================
Provides intelligent fault diagnosis for detected anomalies.

Priority chain:
  1. Ollama local LLM API (mistral/llama3 if running on localhost:11434)
  2. Rule-based expert system fallback (always works offline)

Used by backend.py /diagnose endpoint AND can run standalone:
  python llm_diagnostics.py --reason "CELL_3_VOLT_LOW(1.25V)"
"""

import requests
import json
import re
import sys
import argparse

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "mistral"   # or llama3, phi3 — whatever is pulled locally
TIMEOUT_SEC  = 8.0

# ── System prompt for the LLM ─────────────────────────────────────────────────
SYSTEM_PROMPT = """You are EV Guardian AI, an expert Battery Management Assistant running completely offline on an edge device.

Your job is to analyze real-time battery telemetry and answer user questions about battery condition, safety, health, and maintenance.

You specialize only in:
1. Battery State of Health (SOH)
2. Remaining Useful Life (RUL)
3. Battery Life Cycle Estimation
4. Cell Voltage Analysis
5. Current Analysis
6. Temperature Analysis
7. Cell Balancing
8. Sensor Fault Detection
9. Thermal Runaway Detection
10. Charging Recommendations
11. Maintenance Recommendations
12. Fleet Battery Analytics
13. Battery Aging Mechanisms
14. Battery Safety Warnings

Battery Type:
Lithium Iron Phosphate (LiFePO4)

Rules:
1. Always use the provided telemetry data.
2. Never invent values that are not given.
3. Distinguish between actual battery faults, sensor failures, connector issues, and thermal runaway risks.
4. Explain your reasoning clearly.
5. Give confidence levels (High, Medium, Low).
6. Suggest maintenance actions whenever necessary.
7. Keep responses concise and technical.

Response Format:
Battery Status:
- Healthy / Warning / Critical

Analysis:
- Explain the issue using actual sensor values.

SOH:
- Current SOH estimate (%)

RUL:
- Estimated Remaining Useful Life

Life Cycles Remaining:
- Estimated cycles left

Root Cause:
- Possible reasons for the issue

Recommended Actions:
1.
2.
3.

Confidence:
- High / Medium / Low

If insufficient data exists, say:
"Insufficient telemetry data to determine the battery condition."

Never answer unrelated questions outside battery systems."""

# ── Ollama LLM Call ───────────────────────────────────────────────────────────
def query_ollama(fault_reason: str, model_name: str = OLLAMA_MODEL, temperature: float = 0.7, system_prompt: str = SYSTEM_PROMPT) -> str | None:
    prompt = f"{system_prompt}\n\nFault Code: {fault_reason}\n\nDiagnosis:"
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": model_name,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_gpu": -1,       # Force Ollama to offload model layers to GPU
                    "num_thread": 8,     # Optimize CPU thread count for performance
                    "temperature": temperature
                }
            },
            timeout=TIMEOUT_SEC
        )
        if resp.status_code == 200:
            result = resp.json()
            text = result.get("response", "").strip()
            return text if len(text) > 20 else None
    except Exception:
        pass
    return None

# ── Rule-Based Expert System ──────────────────────────────────────────────────
RULE_DB = {
    "VOLT_LOW": {
        "cause": (
            "A cell voltage below 2.5V is a strong indicator of a wire harness disconnect, "
            "BMS measurement channel failure, or deep over-discharge of the affected cell string."
        ),
        "immediate": [
            "Isolate the vehicle: disable HV contactor to prevent further discharge.",
            "Check cell connector pins J3/J4 on BMS board for corrosion or pull-out.",
            "Do NOT attempt to charge until the root cause is confirmed.",
        ],
        "fix": (
            "If connector is intact, perform capacity test on isolated cell. "
            "Replace cell group if capacity < 70% of nominal. "
            "If sensor channel is faulty, replace BMS sensor harness (P/N: BMS-SH-004)."
        )
    },
    "TEMP_HIGH": {
        "cause": (
            "Cell temperature above 60°C indicates thermal runaway onset, "
            "possibly driven by internal resistance increase, coolant flow blockage, "
            "or an accelerating exothermic side reaction."
        ),
        "immediate": [
            "Activate auxiliary cooling fans immediately; reduce discharge current to <0.5C.",
            "If temperature exceeds 80°C, trigger emergency shutdown and vent battery bay.",
            "Alert the driver to pull over safely and exit the vehicle.",
        ],
        "fix": (
            "Inspect coolant loop for blockage or pump failure. "
            "Check cell swelling — pouch deformation >3mm requires cell replacement. "
            "Review last 10 charge cycles for over-voltage events that may have accelerated degradation."
        )
    },
    "GAS_HIGH": {
        "cause": (
            "Elevated gas sensor reading (>50ppm) indicates electrolyte vapour release, "
            "consistent with internal short circuit, lithium plating, or extreme over-charge."
        ),
        "immediate": [
            "Immediately cut power and ventilate the battery enclosure.",
            "Evacuate the area — do not create ignition sources (sparks, flames).",
            "Contact emergency services if smoke or visible heat is present.",
        ],
        "fix": (
            "Full teardown inspection required. "
            "Replace affected cells and inspect separator integrity. "
            "Review charger EVSE parameters for voltage clamping failures."
        )
    },
    "MODEL_ANOMALY": {
        "cause": (
            "The Isolation Forest AI model flagged a statistically unusual multi-variate "
            "combination of readings that does not match the healthy baseline distribution, "
            "even though no single threshold was breached."
        ),
        "immediate": [
            "Monitor telemetry closely for the next 60 seconds for escalation.",
            "Log the event for fleet-level analysis via Cloud AI 100 endpoint.",
        ],
        "fix": (
            "If anomaly persists for >5 minutes, schedule a preventive maintenance inspection. "
            "Re-train the ONNX model monthly with fresh baseline data to account for battery aging."
        )
    },
}

def _match_rule(fault_reason: str) -> dict | None:
    r = fault_reason.upper()
    
    # Priority 1: Check for critical cell temperatures first (highest priority safety threat)
    if "TEMP" in r and ("HIGH" in r or "115" in r or "OVERHEAT" in r or "HOT" in r or "RUNAWAY" in r):
        return RULE_DB["TEMP_HIGH"]
        
    # Priority 2: Check for low cell voltages (disconnected wiring or deep discharge)
    if "VOLT" in r and ("LOW" in r or "1.2" in r or "DROP" in r or "UNBALANCED" in r):
        return RULE_DB["VOLT_LOW"]
        
    # Priority 3: Check for high off-gas detection
    if "GAS" in r and ("HIGH" in r or "PPM" in r or "LEAK" in r or "ELECTROLYTE" in r):
        return RULE_DB["GAS_HIGH"]
        
    # Check exact keys in database
    for key, rule in RULE_DB.items():
        if key in r:
            return rule
            
    # Try general models anomaly
    if "ANOMALY" in r or "MODEL" in r:
        return RULE_DB["MODEL_ANOMALY"]
        
    return None

def format_rule_response(rule: dict) -> str:
    actions = "\n".join(f"  • {a}" for a in rule["immediate"])
    return (
        f"ROOT CAUSE: {rule['cause']}\n\n"
        f"IMMEDIATE ACTIONS:\n{actions}\n\n"
        f"RECOMMENDED FIX: {rule['fix']}"
    )

# ── Public API ────────────────────────────────────────────────────────────────
def get_diagnosis(fault_reason: str, model_name: str = OLLAMA_MODEL, temperature: float = 0.7, system_prompt: str = SYSTEM_PROMPT) -> str:
    """
    Main entry point called by backend.py HTTP API.
    Returns a diagnosis string.
    """
    # Try LLM first
    llm_result = query_ollama(fault_reason, model_name, temperature, system_prompt)
    if llm_result:
        return f"[LLM:{model_name}] {llm_result}"

    # Fall back to rule engine
    rule = _match_rule(fault_reason)
    if rule:
        return format_rule_response(rule)

    return (
        f"UNKNOWN FAULT: '{fault_reason}'\n"
        "No specific rule matched. Review all sensor channels manually.\n"
        "Inspect BMS firmware event log and compare against vehicle service manual."
    )

# ── CLI Standalone Mode ───────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Guardian LLM Diagnostics")
    parser.add_argument("--reason", default="CELL_3_VOLT_LOW(1.25V)",
                        help="Fault reason code from backend alert")
    args = parser.parse_args()

    print("=" * 65)
    print("  EV Guardian — LLM Diagnostic Copilot")
    print("=" * 65)
    print(f"  Query: {args.reason}\n")

    # Check Ollama availability
    try:
        r = requests.get("http://localhost:11434", timeout=2)
        print(f"  [OLLAMA] Server reachable — using model: {OLLAMA_MODEL}")
    except Exception:
        print(f"  [OLLAMA] Not running — using rule-based fallback engine")

    print()
    result = get_diagnosis(args.reason)
    print(result)
    print("\n" + "=" * 65)
