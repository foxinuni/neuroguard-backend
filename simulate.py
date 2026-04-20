#!/usr/bin/env python3
"""
NeuroGuard — Simulador MQTT + Seeder Firestore
==============================================

Modos:
  python simulate.py seed    → Inyecta 4 semanas de datos históricos directamente en Firestore
  python simulate.py live    → Simulación en tiempo real vía MQTT (crisis cada ~20 min)
  python simulate.py all     → seed + live secuencialmente (recomendado para desarrollo del dashboard)
  python simulate.py clear   → Borra todos los datos generados por el seed

Variables de entorno opcionales:
  SIM_CRISIS_INTERVAL_MIN    Crisis cada N minutos en modo live (default: 20)

Requisitos previos:
  pip install paho-mqtt==2.1.0 firebase-admin==6.5.0 python-dotenv==1.0.1
  .env y firebase-credentials.json deben estar en la misma carpeta que este script.
"""

import sys
import os
import json
import time
import math
import random
import ssl
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import paho.mqtt.client as mqtt
import firebase_admin
from firebase_admin import credentials, firestore as fsdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("neuroguard.sim")

# ── Identidad ──────────────────────────────────────────────────────────────────
PATIENT_ID = "paciente_001"
DEVICE_ID  = "esp32_001"

# ── MQTT ───────────────────────────────────────────────────────────────────────
MQTT_HOST      = os.getenv("MQTT_HOST", "b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud")
MQTT_PORT      = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER      = os.getenv("MQTT_USER", "Iot2026")
MQTT_PASSWORD  = os.getenv("MQTT_PASSWORD", "Qwert1234")
MQTT_CLIENT_ID = "neuroguard-simulator-01"

TOPIC_TELEMETRY = f"neuroguard/{PATIENT_ID}/{DEVICE_ID}/telemetry"
TOPIC_STATUS    = f"neuroguard/{PATIENT_ID}/{DEVICE_ID}/status"

# ── Firebase ───────────────────────────────────────────────────────────────────
FIREBASE_CREDS = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    str(Path(__file__).parent / "firebase-credentials.json"),
)

# ── Parámetros de simulación ───────────────────────────────────────────────────
SEED_WEEKS               = 4
CRISES_TOTAL             = 18      # en las 4 semanas (~18/mes como muestra el mockup)
NOCTURNAL_RATIO          = 0.68    # 68 % nocturnas (22:00–06:00)
BASELINE_HR              = 72.0    # bpm basal del paciente
BASELINE_SPO2            = 97.0    # % SpO2 basal
LIVE_CRISIS_INTERVAL_MIN = int(os.getenv("SIM_CRISIS_INTERVAL_MIN", "20"))


# ==============================================================================
#  GENERADOR DE SEÑALES FISIOLÓGICAS
# ==============================================================================

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _noise(sigma: float) -> float:
    return random.gauss(0.0, sigma)

def _circadian_hr(hour: int) -> float:
    """±6 bpm variación circadiana. Pico ~14 h, nadir ~04 h."""
    return 6.0 * math.sin(2.0 * math.pi * (hour - 4) / 24.0)


def gen_reading(phase: str, t: float, hour: int, sev: str = "medium") -> dict:
    """
    Genera un payload de telemetría simulado.

    phase : "normal" | "pre" | "tonic" | "clonic" | "post"
    t     : segundos transcurridos dentro de la fase actual
    hour  : hora del día (0–23) para modulación circadiana
    sev   : "low" | "medium" | "high"

    Umbrales de detección del backend:
      acc_mag > 2.0 g  ó  gyro_mag > 150 °/s  →  motor elevado
      hr > 120 bpm     ó  spo2 < 90 %          →  señal fisiológica
    """
    m = {"low": 0.75, "medium": 1.0, "high": 1.30}[sev]

    if phase == "normal":
        acc  = _clamp(1.0  + _noise(0.08),       0.85, 1.25)
        gyro = _clamp(abs(12.0 + _noise(5.0)),   2.0,  45.0)
        hr   = _clamp(BASELINE_HR + _circadian_hr(hour) + _noise(3.0), 55.0,  95.0)
        spo2 = _clamp(BASELINE_SPO2 + _noise(0.5),                     94.5,  99.5)

    elif phase == "pre":
        # Pre-ictal: elevación gradual durante ~45 s
        r    = _clamp(t / 45.0, 0.0, 1.0)
        acc  = _clamp(1.0  + r * 0.55 * m + _noise(0.10),  0.9,  2.0)
        gyro = _clamp(12.0 + r * 65.0  * m + _noise(8.0),  5.0,  130.0)
        hr   = _clamp(BASELINE_HR + r * 38.0 * m + _noise(4.0), 65.0, 135.0)
        spo2 = _clamp(BASELINE_SPO2 - r * 2.0 * m + _noise(0.4), 93.0, 99.0)

    elif phase == "tonic":
        # Fase tónica: rigidez intensa, amplitud alta
        acc  = _clamp(3.8  * m + _noise(0.6),  2.0,   6.5)
        gyro = _clamp(300.0 * m + _noise(50),  150.0, 650.0)
        hr   = _clamp(148.0 * m + _noise(8),   110.0, 180.0)
        spo2 = _clamp(88.0 - (m - 1.0) * 6.0 + _noise(2.0), 76.0, 94.0)

    elif phase == "clonic":
        # Fase clónica: movimientos rítmicos 3–5 Hz con fatiga progresiva
        freq    = random.uniform(3.0, 5.0)
        fatigue = math.exp(-t / 90.0)
        osc     = abs(math.sin(2.0 * math.pi * freq * t))
        acc  = _clamp((1.8  + 2.2  * osc * fatigue) * m + _noise(0.35), 1.0,   6.5)
        gyro = _clamp((80.0 + 240.0 * osc * fatigue) * m + _noise(25),  40.0,  550.0)
        hr   = _clamp(158.0 * m - t * 0.12 + _noise(6),                 110.0, 185.0)
        spo2 = _clamp(80.0  - (m - 1.0) * 8.0 + _noise(2.0),           70.0,  92.0)

    elif phase == "post":
        # Post-ictal: agotamiento + recuperación gradual en ~5 min
        r    = _clamp(t / 300.0, 0.0, 1.0)
        acc  = _clamp(0.96 + _noise(0.06),          0.84, 1.15)
        gyro = _clamp(abs(6.0 + _noise(3.0)),        1.0,  20.0)
        hr   = _clamp(118.0 - r * 50.0 * m + _noise(5.0), 60.0, 130.0)
        spo2 = _clamp(87.0  + r * 10.0 + _noise(1.5),     83.0,  99.0)

    else:
        raise ValueError(f"Fase desconocida: '{phase}'")

    # ── Derivar ejes individuales desde la magnitud ────────────────────────────
    ax = _noise(acc * 0.45)
    ay = _noise(acc * 0.45)
    az = math.copysign(
        math.sqrt(max(0.0, acc ** 2 - ax ** 2 - ay ** 2)),
        random.choice([1, -1]),
    )
    gx = _noise(gyro * 0.50)
    gy = _noise(gyro * 0.50)
    gz = _noise(gyro * 0.35)
    gm = math.sqrt(gx ** 2 + gy ** 2 + gz ** 2)
    if gm > 0:
        gx, gy, gz = gx / gm * gyro, gy / gm * gyro, gz / gm * gyro

    # ── Óptica cruda (correlacionada con SpO2) ─────────────────────────────────
    ir  = int(_clamp(120_000 + _noise(5_000),                            80_000, 160_000))
    red = int(_clamp(ir * (0.90 - (99.0 - spo2) * 0.015) + _noise(2_000), 55_000, 140_000))

    return {
        "device": DEVICE_ID,
        "imu": {
            "ax": round(ax,  4), "ay": round(ay,  4), "az": round(az,  4),
            "acc_mag":  round(acc,  4),
            "gx": round(gx,  3), "gy": round(gy,  3), "gz": round(gz,  3),
            "gyro_mag": round(gyro, 3),
        },
        "max30102": {
            "ir": ir, "red": red,
            "hr":     round(hr,   2),
            "spo2":   round(spo2, 2),
            "finger": True,
        },
    }


# ==============================================================================
#  SEEDER DE FIRESTORE (datos históricos con timestamps reales del pasado)
# ==============================================================================

class FirestoreSeeder:
    """
    Inyecta SEED_WEEKS semanas de datos históricos directamente en Firestore,
    respetando la misma estructura que genera el backend real.
    """

    def __init__(self):
        if not firebase_admin._apps:
            if not Path(FIREBASE_CREDS).exists():
                log.error(f"No se encontró: {FIREBASE_CREDS}")
                sys.exit(1)
            cred = credentials.Certificate(FIREBASE_CREDS)
            firebase_admin.initialize_app(cred)
        self.db           = fsdb.client()
        self.patient_ref  = self.db.collection("patients").document(PATIENT_ID)
        self.device_ref   = self.patient_ref.collection("devices").document(DEVICE_ID)
        self.events_ref   = self.patient_ref.collection("events")
        self.readings_ref = self.device_ref.collection("readings")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _batch_commit(self, ops: list):
        """Escribe en Firestore en lotes de 400 (límite oficial = 500 por batch)."""
        BATCH_SIZE = 400
        total = len(ops)
        for i in range(0, total, BATCH_SIZE):
            batch = self.db.batch()
            for ref, doc in ops[i: i + BATCH_SIZE]:
                batch.set(ref, doc)
            batch.commit()
            log.info(f"    [{min(i + BATCH_SIZE, total):>5}/{total}] escrituras en Firestore")

    @staticmethod
    def _is_night(hour: int) -> bool:
        return hour >= 22 or hour < 6

    # ── Programación de crisis ─────────────────────────────────────────────────

    def _crisis_schedule(self, now: datetime) -> list:
        """
        Genera lista de crisis distribuidas en SEED_WEEKS semanas anteriores a 'now'.
        Respeta el ratio nocturno/diurno. Reproducible con semilla fija (42).

        Retorna: lista de dicts ordenados por start_dt.
        """
        rng        = random.Random(42)
        seed_start = now - timedelta(weeks=SEED_WEEKS)
        total_s    = SEED_WEEKS * 7 * 24 * 3600

        n_noc = round(CRISES_TOTAL * NOCTURNAL_RATIO)   # 12
        n_day = CRISES_TOTAL - n_noc                     # 6

        def rand_dt(night: bool) -> datetime:
            for _ in range(2000):
                offset = rng.uniform(0, total_s)
                dt = seed_start + timedelta(seconds=offset)
                if night == self._is_night(dt.hour):
                    return dt
            return seed_start + timedelta(seconds=rng.uniform(0, total_s))

        crises = []
        for _ in range(n_noc):
            crises.append({
                "start_dt":    rand_dt(True),
                "duration_s":  rng.randint(60, 195),
                "severity":    rng.choices(["low", "medium", "high"], weights=[2, 5, 3])[0],
                "is_nocturnal": True,
            })
        for _ in range(n_day):
            crises.append({
                "start_dt":    rand_dt(False),
                "duration_s":  rng.randint(45, 150),
                "severity":    rng.choices(["low", "medium", "high"], weights=[3, 5, 2])[0],
                "is_nocturnal": False,
            })

        crises.sort(key=lambda c: c["start_dt"])
        log.info(f"  Programadas {len(crises)} crisis "
                 f"({n_noc} nocturnas / {n_day} diurnas)")
        return crises

    # ── Lecturas de una crisis ─────────────────────────────────────────────────

    def _crisis_readings(self, crisis: dict, ev_id: str) -> list:
        """
        Lecturas cada 2.5 s cubriendo:
          45 s pre-ictal  |  duración (tónica + clónica)  |  4 min post-ictal
        Incluye 'crisis_id' para facilitar consultas del dashboard.
        """
        start   = crisis["start_dt"]
        dur     = crisis["duration_s"]
        sev     = crisis["severity"]
        tonic_s = min(40, dur // 3)

        out   = []
        t_cur = -45.0
        while t_cur < dur + 240:
            if t_cur < 0:
                phase, t_ph = "pre",    t_cur + 45.0
            elif t_cur < tonic_s:
                phase, t_ph = "tonic",  t_cur
            elif t_cur < dur:
                phase, t_ph = "clonic", t_cur - tonic_s
            else:
                phase, t_ph = "post",   t_cur - dur

            dt = start + timedelta(seconds=t_cur)
            r  = gen_reading(phase, t_ph, start.hour, sev)
            r["crisis_id"]    = ev_id   # link al evento
            r["crisis_phase"] = phase   # útil para el dashboard
            out.append((dt, r))
            t_cur += 2.5

        return out

    # ── Documento de evento ────────────────────────────────────────────────────

    @staticmethod
    def _build_event(crisis: dict, readings: list) -> dict:
        start = crisis["start_dt"]
        end   = start + timedelta(seconds=crisis["duration_s"])

        all_acc  = [r["imu"]["acc_mag"]   for _, r in readings]
        all_gyro = [r["imu"]["gyro_mag"]  for _, r in readings]
        all_hr   = [r["max30102"]["hr"]   for _, r in readings]
        all_spo2 = [r["max30102"]["spo2"] for _, r in readings]

        ictal_acc = [r["imu"]["acc_mag"] for dt, r in readings if start <= dt <= end]
        if not ictal_acc:
            ictal_acc = all_acc
        pct_motor = sum(1 for v in ictal_acc if v > 2.0) / len(ictal_acc)

        return {
            "type":             "possible_tonic_clonic",
            "severity":         crisis["severity"],
            "source":           "backend_simulated",
            "timestamp":        start.isoformat(),
            "start_timestamp":  start.isoformat(),
            "end_timestamp":    end.isoformat(),
            "duration_seconds": crisis["duration_s"],
            "is_nocturnal":     crisis["is_nocturnal"],
            "device_id":        DEVICE_ID,
            "motor": {
                "pct_elevated":  round(pct_motor * 100, 1),
                "acc_mag_max":   round(max(all_acc),  3),
                "acc_mag_mean":  round(sum(all_acc)  / len(all_acc),  3),
                "gyro_mag_max":  round(max(all_gyro), 3),
                "gyro_mag_mean": round(sum(all_gyro) / len(all_gyro), 3),
            },
            "physiological": {
                "hr_basal_bpm": BASELINE_HR,
                "hr_peak_bpm":  round(max(all_hr),   1),
                "hr_elevated":  max(all_hr) > 120,
                "spo2_min":     round(min(all_spo2), 1),
                "spo2_low":     min(all_spo2) < 90,
            },
        }

    # ── Seed principal ─────────────────────────────────────────────────────────

    def seed(self):
        now    = datetime.now(timezone.utc)
        crises = self._crisis_schedule(now)

        reading_ops: list = []
        event_ops:   list = []
        crisis_windows    = []   # (start_dt - 90s, end_dt + 300s) para excluir del baseline
        latest_doc        = None
        last_event_ts     = None
        last_event_id     = None

        log.info("Generando lecturas de crisis...")
        for idx, crisis in enumerate(crises):
            sdt = crisis["start_dt"].strftime("%Y-%m-%d %H:%M")
            log.info(f"  Crisis {idx + 1:>2}/{len(crises)}  {sdt}  "
                     f"dur={crisis['duration_s']:>3}s  sev={crisis['severity']}")

            # Reservar ID del evento antes de generar las lecturas
            ev_ref  = self.events_ref.document()
            ev_id   = ev_ref.id
            readings = self._crisis_readings(crisis, ev_id)
            event    = self._build_event(crisis, readings)

            event_ops.append((ev_ref, event))

            ts = crisis["start_dt"].isoformat()
            if last_event_ts is None or ts > last_event_ts:
                last_event_ts = ts
                last_event_id = ev_id

            for dt, r in readings:
                doc = {**r,
                       "timestamp":  dt.isoformat(),
                       "patient_id": PATIENT_ID,
                       "device_id":  DEVICE_ID}
                reading_ops.append((self.readings_ref.document(), doc))
                latest_doc = doc   # se sobreescribe; el más reciente gana

            crisis_windows.append((
                crisis["start_dt"] - timedelta(seconds=90),
                crisis["start_dt"] + timedelta(seconds=crisis["duration_s"] + 300),
            ))

        # ── Lecturas baseline (cada 15 min, fuera de ventanas de crisis) ────────
        log.info("Generando lecturas baseline (cada 15 min)...")
        seed_start = now - timedelta(weeks=SEED_WEEKS)
        t_cur      = seed_start.replace(second=0, microsecond=0)
        baseline_n = 0
        while t_cur < now:
            in_crisis = any(s <= t_cur <= e for s, e in crisis_windows)
            if not in_crisis:
                r   = gen_reading("normal", 0.0, t_cur.hour)
                doc = {**r,
                       "timestamp":  t_cur.isoformat(),
                       "patient_id": PATIENT_ID,
                       "device_id":  DEVICE_ID}
                reading_ops.append((self.readings_ref.document(), doc))
                latest_doc  = doc
                baseline_n += 1
            t_cur += timedelta(minutes=15)

        dense_n = len(reading_ops) - baseline_n
        log.info(f"  Readings: {len(reading_ops)} total "
                 f"({dense_n} ventana-ictal + {baseline_n} baseline)")
        log.info(f"  Eventos:  {len(event_ops)}")

        # ── Escribir en Firestore ────────────────────────────────────────────
        log.info("Commiteando readings en Firestore...")
        self._batch_commit(reading_ops)

        log.info("Commiteando eventos en Firestore...")
        self._batch_commit(event_ops)

        # ── Metadatos del paciente ───────────────────────────────────────────
        self.patient_ref.set({
            "patient_id":           PATIENT_ID,
            "name":                 "Paciente Demo",
            "epilepsy_type":        "Epilepsia generalizada tónico-clónica",
            "basal_hr":             BASELINE_HR,
            "last_event_timestamp": last_event_ts,
            "last_event_id":        last_event_id,
        }, merge=True)

        # ── latest/current para el dashboard en tiempo real ──────────────────
        if latest_doc:
            self.device_ref.collection("latest").document("current").set(latest_doc)

        # ── Estado del dispositivo ───────────────────────────────────────────
        self.db.collection("devices").document(DEVICE_ID).set({
            "status":     "online",
            "patient_id": PATIENT_ID,
            "last_seen":  now.isoformat(),
        }, merge=True)

        log.info("✓ Seed completado exitosamente.")

    # ── Clear ──────────────────────────────────────────────────────────────────

    def clear(self):
        """Elimina todas las readings y eventos del paciente simulado."""
        log.warning("Borrando datos de Firestore...")
        for coll_ref, name in [
            (self.readings_ref, "readings"),
            (self.events_ref,   "events"),
        ]:
            deleted = 0
            while True:
                docs = list(coll_ref.limit(400).stream())
                if not docs:
                    break
                batch = self.db.batch()
                for d in docs:
                    batch.delete(d.reference)
                batch.commit()
                deleted += len(docs)
                log.info(f"  {name}: {deleted} documentos borrados")
            log.info(f"  ✓ {name} limpia.")
        log.info("✓ Clear completado.")


# ==============================================================================
#  SIMULADOR MQTT EN TIEMPO REAL
# ==============================================================================

class MQTTSimulator:
    """
    Publica mensajes al broker HiveMQ a 500 ms/lectura como si fuera el ESP32.
    El backend existente los procesa y los escribe en Firestore normalmente.
    Genera una crisis cada ~LIVE_CRISIS_INTERVAL_MIN minutos.
    """

    def __init__(self):
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv5,
        )
        self.client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.tls_insecure_set(False)
        self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    @staticmethod
    def _on_connect(client, userdata, flags, rc, properties=None):
        ok = (not rc.is_failure) if hasattr(rc, "is_failure") else (rc == 0)
        if ok:
            log.info("Simulador MQTT conectado ✓")
            client.publish(
                TOPIC_STATUS,
                json.dumps({"status": "online", "device": DEVICE_ID}),
                qos=1, retain=True,
            )
        else:
            log.error(f"Fallo de conexión MQTT: {rc}")

    @staticmethod
    def _on_disconnect(client, userdata, flags, rc, properties=None):
        log.warning(f"Simulador MQTT desconectado (rc={rc})")

    def connect(self):
        log.info(f"Conectando simulador a {MQTT_HOST}:{MQTT_PORT}...")
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()
        time.sleep(2.0)   # esperar confirmación de conexión

    def _pub(self, phase: str, t_ph: float, sev: str = "medium"):
        hour    = datetime.now(timezone.utc).hour
        payload = gen_reading(phase, t_ph, hour, sev)
        self.client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=0)

    def _run_phase(self, phase: str, duration_s: float, sev: str = "medium",
                   interval_s: float = 0.5):
        t = 0.0
        while t < duration_s:
            self._pub(phase, t, sev)
            time.sleep(interval_s)
            t += interval_s

    def simulate_crisis(self, sev: str = "medium"):
        dur_total = random.randint(60, 180)
        tonic_s   = min(40, dur_total // 3)
        clonic_s  = dur_total - tonic_s
        log.warning(f"▶ CRISIS  sev={sev}  duración={dur_total}s  "
                    f"(tónica={tonic_s}s  clónica={clonic_s}s)")
        self._run_phase("pre",    45.0,    sev)
        self._run_phase("tonic",  tonic_s, sev)
        self._run_phase("clonic", clonic_s, sev)
        self._run_phase("post",   180.0,   sev)   # 3 min post-ictal
        log.warning("■ FIN CRISIS")

    def run(self):
        self.connect()
        interval_min = LIVE_CRISIS_INTERVAL_MIN
        log.info(f"Simulador en vivo activo — crisis cada ~{interval_min} min "
                 f"| Ctrl+C para detener")

        t_next = time.time() + interval_min * 60
        t_ph   = 0.0

        try:
            while True:
                if time.time() >= t_next:
                    sev = random.choices(
                        ["low", "medium", "medium", "high"], weights=[2, 4, 4, 2]
                    )[0]
                    self.simulate_crisis(sev)
                    jitter = random.randint(-120, 120)
                    t_next = time.time() + interval_min * 60 + jitter
                    t_ph   = 0.0
                else:
                    self._pub("normal", t_ph)
                    time.sleep(0.5)
                    t_ph += 0.5
        except KeyboardInterrupt:
            log.info("Simulador detenido por el usuario.")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


# ==============================================================================
#  ENTRY POINT
# ==============================================================================

def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "help"

    if mode == "seed":
        log.info("══ Modo SEED ════════════════════════════════════════════")
        FirestoreSeeder().seed()

    elif mode == "live":
        log.info("══ Modo LIVE ════════════════════════════════════════════")
        MQTTSimulator().run()

    elif mode == "all":
        log.info("══ Modo ALL: seed → live ════════════════════════════════")
        FirestoreSeeder().seed()
        log.info("Seed listo. Iniciando simulador en vivo...")
        MQTTSimulator().run()

    elif mode == "clear":
        log.info("══ Modo CLEAR ═══════════════════════════════════════════")
        confirm = input("¿Borrar todos los datos de simulación? [s/N]: ")
        if confirm.strip().lower() == "s":
            FirestoreSeeder().clear()
        else:
            log.info("Operación cancelada.")

    else:
        print(__doc__)
        print("Uso:  python simulate.py [seed | live | all | clear]")
        sys.exit(1)


if __name__ == "__main__":
    main()
