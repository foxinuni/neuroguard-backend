"""
NeuroGuard Crisis Simulator
===========================
Simulates an ESP32 wearable publishing telemetry to HiveMQ, triggering the
backend crisis detector and allowing end-to-end testing of the full system:

  ESP32 (simulated) → HiveMQ → backend → Firestore → dashboard + Flutter app

Usage
-----
  python scripts/simulate_crisis.py [OPTIONS]

  Options:
    --patient   Patient ID in Firestore  (default: paciente_001)
    --device    Device ID               (default: esp32_001)
    --severity  Crisis severity to aim for: low | medium | high  (default: medium)
    --no-color  Disable colored terminal output

The script automatically connects using the production HiveMQ credentials.
"""

import argparse
import json
import math
import os
import random
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# MQTT / broker settings (same as backend)
# ---------------------------------------------------------------------------
MQTT_HOST      = os.getenv("MQTT_HOST",     "b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud")
MQTT_PORT      = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER      = os.getenv("MQTT_USER",     "Iot2026")
MQTT_PASSWORD  = os.getenv("MQTT_PASSWORD", "Qwert1234")
MQTT_CLIENT_ID = "neuroguard-simulator"

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    WHITE  = "\033[37m"
    GREY   = "\033[90m"

    @classmethod
    def disable(cls):
        for attr in ("RESET","BOLD","RED","GREEN","YELLOW","CYAN","WHITE","GREY"):
            setattr(cls, attr, "")


# ---------------------------------------------------------------------------
# Telemetry generation
# ---------------------------------------------------------------------------
@dataclass
class PhaseParams:
    """Parameters that define a simulation phase."""
    name:         str
    duration_s:   float
    acc_mag_min:  float
    acc_mag_max:  float
    gyro_mag_min: float
    gyro_mag_max: float
    hr_min:       float
    hr_max:       float
    spo2_min:     float
    spo2_max:     float
    finger:       bool = True


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _jitter(value: float, pct: float = 0.05) -> float:
    return value * (1.0 + random.uniform(-pct, pct))


def build_phases(severity: str) -> list[PhaseParams]:
    """Return four simulation phases tuned to the requested severity level.

    Crisis phase parameters are chosen so that the backend CrisisDetector
    triggers within the 20-second crisis window:
      - ≥60 % of samples have acc_mag > 2.0 g  OR  gyro_mag > 150 °/s
      - HR > 120 bpm  OR  SpO2 < 90 %
    """
    # Physiological targets per severity ─────────────────────────────────────
    #  low    → barely crosses thresholds (score ≤ 1 in detector)
    #  medium → moderate elevation (score 2–3)
    #  high   → strong elevation   (score ≥ 4)
    profiles = {
        "low": dict(
            crisis_acc=(2.1, 2.6), crisis_gyro=(155, 175),
            crisis_hr=(121, 128),  crisis_spo2=(88, 90),
        ),
        "medium": dict(
            crisis_acc=(2.4, 3.2), crisis_gyro=(160, 200),
            crisis_hr=(125, 138),  crisis_spo2=(85, 89),
        ),
        "high": dict(
            crisis_acc=(2.8, 3.8), crisis_gyro=(180, 240),
            crisis_hr=(141, 158),  crisis_spo2=(81, 86),
        ),
    }
    p = profiles.get(severity, profiles["medium"])

    return [
        PhaseParams(
            name="Normal",
            duration_s=15,
            acc_mag_min=0.8,    acc_mag_max=1.1,
            gyro_mag_min=10,    gyro_mag_max=30,
            hr_min=68,          hr_max=76,
            spo2_min=96,        spo2_max=98,
        ),
        PhaseParams(
            name="Pre-crisis",
            duration_s=10,
            acc_mag_min=1.4,    acc_mag_max=2.2,
            gyro_mag_min=70,    gyro_mag_max=145,
            hr_min=100,         hr_max=115,
            spo2_min=92,        spo2_max=95,
        ),
        PhaseParams(
            name="CRISIS",
            duration_s=20,
            acc_mag_min=p["crisis_acc"][0],    acc_mag_max=p["crisis_acc"][1],
            gyro_mag_min=p["crisis_gyro"][0],  gyro_mag_max=p["crisis_gyro"][1],
            hr_min=p["crisis_hr"][0],          hr_max=p["crisis_hr"][1],
            spo2_min=p["crisis_spo2"][0],      spo2_max=p["crisis_spo2"][1],
        ),
        PhaseParams(
            name="Recuperación",
            duration_s=10,
            acc_mag_min=0.9,    acc_mag_max=1.3,
            gyro_mag_min=15,    gyro_mag_max=50,
            hr_min=78,          hr_max=95,
            spo2_min=94,        spo2_max=97,
        ),
    ]


def generate_reading(phase: PhaseParams, step: int, total_steps: int, device_id: str) -> dict:
    """Generate one telemetry reading for the given phase."""
    t = step / max(total_steps - 1, 1)   # 0.0 → 1.0 progress within phase

    acc_mag  = _jitter(_lerp(phase.acc_mag_min,  phase.acc_mag_max,  t))
    gyro_mag = _jitter(_lerp(phase.gyro_mag_min, phase.gyro_mag_max, t))
    hr       = _jitter(_lerp(phase.hr_min,       phase.hr_max,       t))
    spo2     = _jitter(_lerp(phase.spo2_min,     phase.spo2_max,     t), pct=0.02)

    # Decompose magnitudes into plausible axis components
    theta = random.uniform(0, 2 * math.pi)
    phi   = random.uniform(0, math.pi)
    ax    = round(acc_mag * math.sin(phi) * math.cos(theta), 4)
    ay    = round(acc_mag * math.sin(phi) * math.sin(theta), 4)
    az    = round(acc_mag * math.cos(phi), 4)

    theta2 = random.uniform(0, 2 * math.pi)
    gx = round(gyro_mag * math.cos(theta2), 3)
    gy = round(gyro_mag * math.sin(theta2), 3)
    gz = round(random.uniform(-gyro_mag * 0.2, gyro_mag * 0.2), 3)

    ir  = int(100000 + spo2 * 1000 + random.uniform(-300, 300))
    red = int(ir * (1 - (100 - spo2) / 200))

    return {
        "device": device_id,
        "imu": {
            "ax": ax, "ay": ay, "az": az,
            "acc_mag":  round(acc_mag, 4),
            "gx": gx,  "gy": gy,  "gz": gz,
            "gyro_mag": round(gyro_mag, 3),
        },
        "max30102": {
            "ir":     ir,
            "red":    red,
            "hr":     round(hr, 2),
            "spo2":   round(spo2, 2),
            "finger": phase.finger,
        },
    }


# ---------------------------------------------------------------------------
# Progress bar helper
# ---------------------------------------------------------------------------
def _bar(done: int, total: int, width: int = 18) -> str:
    filled  = int(width * done / max(total, 1))
    empty   = width - filled
    return "█" * filled + "░" * empty


def _phase_color(name: str) -> str:
    if "CRISIS" in name:
        return C.RED + C.BOLD
    if "Pre" in name:
        return C.YELLOW
    if "Recup" in name:
        return C.GREEN
    return C.CYAN


# ---------------------------------------------------------------------------
# MQTT connection
# ---------------------------------------------------------------------------
_connected = False

def _on_connect(client, userdata, flags, reason_code, properties=None):
    global _connected
    ok = not (reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0)
    _connected = ok

def _on_disconnect(client, userdata, flags, reason_code, properties=None):
    global _connected
    _connected = False


def connect_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.tls_insecure_set(False)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    deadline = time.time() + 10
    while not _connected and time.time() < deadline:
        time.sleep(0.1)

    if not _connected:
        client.loop_stop()
        raise RuntimeError(f"No se pudo conectar a {MQTT_HOST}:{MQTT_PORT}")

    return client


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------
INTERVAL_S = 0.5   # 500 ms between samples (same as real ESP32)


def run_simulation(patient: str, device: str, severity: str) -> None:
    topic_telemetry = f"neuroguard/{patient}/{device}/telemetry"
    topic_status    = f"neuroguard/{patient}/{device}/status"
    phases          = build_phases(severity)
    total_duration  = sum(p.duration_s for p in phases)

    print(f"\n{C.BOLD}{'─'*58}{C.RESET}")
    print(f"{C.BOLD}  NeuroGuard Crisis Simulator{C.RESET}")
    print(f"{'─'*58}")
    print(f"  Paciente : {C.CYAN}{patient}{C.RESET}")
    print(f"  Dispositivo: {C.CYAN}{device}{C.RESET}")
    print(f"  Severidad   : {C.YELLOW}{severity.upper()}{C.RESET}")
    print(f"  Duración total: {C.WHITE}{total_duration:.0f}s{C.RESET}")
    print(f"{'─'*58}\n")

    # --- connect ---
    print(f"  {C.GREY}Conectando a HiveMQ…{C.RESET}", end="", flush=True)
    try:
        client = connect_mqtt()
    except RuntimeError as exc:
        print(f"\n  {C.RED}✗ {exc}{C.RESET}\n")
        sys.exit(1)
    print(f"\r  {C.GREEN}✓ Conectado al broker MQTT{C.RESET}               ")

    # --- status: online ---
    client.publish(
        topic_status,
        json.dumps({"status": "online", "device_id": device}),
        qos=1,
    )
    print(f"  {C.GREEN}✓ Status → online{C.RESET}\n")

    # --- simulation phases ---
    try:
        for phase_index, phase in enumerate(phases):
            steps = max(1, int(phase.duration_s / INTERVAL_S))
            pc    = _phase_color(phase.name)
            label = f"{pc}{phase.name:<14}{C.RESET}"

            for step in range(steps):
                reading = generate_reading(phase, step, steps, device)
                msg     = json.dumps(reading)
                client.publish(topic_telemetry, msg, qos=1)

                bar     = _bar(step + 1, steps)
                elapsed = f"{(step + 1) * INTERVAL_S:4.1f}s/{phase.duration_s:.0f}s"
                notice  = ""
                if "CRISIS" in phase.name and step == int(steps * 0.65):
                    notice = f"  {C.RED}⚠ detector activo…{C.RESET}"

                print(
                    f"\r  Fase {phase_index + 1}/{len(phases)}: {label} "
                    f"[{bar}] {elapsed}{notice}    ",
                    end="",
                    flush=True,
                )
                time.sleep(INTERVAL_S)

            print()   # newline after each phase

    except KeyboardInterrupt:
        print(f"\n\n  {C.YELLOW}⚡ Simulación interrumpida por el usuario{C.RESET}")

    finally:
        # --- status: offline ---
        client.publish(
            topic_status,
            json.dumps({"status": "offline", "device_id": device}),
            qos=1,
        )
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()

    print(f"\n  {C.GREEN}✓ Simulación completada{C.RESET}")
    print(f"  {C.GREY}Revisa Firestore → patients/{patient}/events/ para el evento generado{C.RESET}")
    print(f"{'─'*58}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Simula telemetría del ESP32 + crisis epiléptica para NeuroGuard"
    )
    parser.add_argument(
        "--patient",   default="paciente_001",
        help="Patient ID (coincide con Firestore). Default: paciente_001"
    )
    parser.add_argument(
        "--device",    default="esp32_001",
        help="Device ID. Default: esp32_001"
    )
    parser.add_argument(
        "--severity",  default="medium", choices=["low", "medium", "high"],
        help="Severidad de la crisis simulada. Default: medium"
    )
    parser.add_argument(
        "--no-color",  action="store_true",
        help="Desactiva colores ANSI en la salida"
    )
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        C.disable()

    run_simulation(args.patient, args.device, args.severity)


if __name__ == "__main__":
    main()
