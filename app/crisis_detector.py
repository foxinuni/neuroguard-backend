import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
import pickle

log = logging.getLogger("neuroguard.detector")

# ──────────────────────────────────────────────
# UMBRALES DE DETECCIÓN DE CRISIS TÓNICO-CLÓNICA
#
# Basados en la literatura clínica del proyecto:
# - Actividad motora elevada: acc_mag > 2.0g (convulsiones generalizadas)
# - Ritmo: gyro_mag > 150 °/s sostenido
# - Respuesta cardiovascular: HR > 120 bpm o caída de SpO2 < 90%
# - Duración mínima: 10 segundos de actividad elevada para evitar falsos positivos
# ──────────────────────────────────────────────

UMBRAL_ACC_MAG   = 2.0    # g — magnitud de aceleración indicativa de convulsión
UMBRAL_GYRO_MAG  = 150.0  # °/s — actividad angular elevada
UMBRAL_HR_ALTO   = 120    # bpm — taquicardia ictal
UMBRAL_SPO2_BAJO = 90.0   # % — desaturación significativa
VENTANA_SEGUNDOS      = 10    # segundos de actividad elevada para confirmar crisis
COOLDOWN_SEGUNDOS     = 60    # segundos mínimos entre dos alertas del mismo dispositivo
SAMPLE_INTERVAL_S     = 0.5   # intervalo de publicación del ESP32 (500 ms)
MIN_WINDOW_FILL_RATIO = 0.70  # la ventana debe estar al menos 70% llena antes de evaluar

class CrisisDetector:
    """
    Detector de posibles crisis tónico-clónicas basado en modelo AI.
    
    Estrategia multimodal:
    1. Analiza la ventana de los últimos N segundos de lecturas.
    2. Evalúa el modelo AI para determinar si hay una crisis.
    3. Un cooldown evita alertas repetidas del mismo evento continuo.
    """

    def __init__(self, device_id: str, window_seconds: int = VENTANA_SEGUNDOS):
        self.device_id      = device_id
        self.window_seconds = window_seconds
        self.buffer: deque  = deque()   # (timestamp, lectura)
        self.last_alert_ts  = 0.0       # timestamp del último evento generado

    def evaluate(self, reading: dict) -> dict | None:
        """
        Evalúa una nueva lectura. Retorna un dict de evento si detecta crisis,
        o None si todo está dentro de rangos normales.
        """
        now = time.time()
        self.buffer.append((now, reading))

        # Limpiar lecturas fuera de la ventana temporal
        cutoff = now - self.window_seconds
        while self.buffer and self.buffer[0][0] < cutoff:
            self.buffer.popleft()

        # ── Reglas de la ventana de segmentación ─────────
        # Regla 1: Ventana temporal deslizante de N segundos.
        # Regla 2: Llenado mínimo del 70% de muestras esperadas antes de evaluar.
        #          A 500ms/muestra y 10s de ventana → ~20 muestras esperadas, mínimo 14.
        # Regla 3: ≥60% de muestras con actividad motora elevada (acc o gyro).
        # Regla 4: Confirmación fisiológica (HR > 120 bpm ó SpO2 < 90%).
        expected_samples = self.window_seconds / SAMPLE_INTERVAL_S
        if len(self.buffer) < expected_samples * MIN_WINDOW_FILL_RATIO:
            return None

        # ── Preparar datos para el modelo AI ──────────────
        features = []
        for _, r in self.buffer:
            imu = r.get("imu", {})
            acc_mag  = imu.get("acc_mag",  0)
            gyro_mag = imu.get("gyro_mag", 0)
            hr       = max30.get("hr", 0)
            spo2     = max30.get("spo2", 100)

            features.append([acc_mag, gyro_mag, hr, spo2])

        # ── Evaluar el modelo AI ───────────────────────
        with open('model.pkl', 'rb') as f:
            model = pickle.load(f)

        prediction = model.predict(features)
        if prediction == 1:
            return self._generate_event(now)
        else:
            return None

    def _generate_event(self, now):
        # ── Construir payload del evento ──────────────────
        imu_vals = [r.get("imu", {}) for _, r in self.buffer]
        acc_vals  = [r.get("acc_mag", 0)  for r in imu_vals]
        gyro_vals = [r.get("gyro_mag", 0) for r in imu_vals]

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        end_dt = now_dt
        start_dt = now_dt - timedelta(seconds=self.window_seconds)
        is_night = now_dt.hour >= 22 or now_dt.hour < 6

        evento = {
            "type":             "possible_tonic_clonic",
            "timestamp":        now_iso,
            "start_timestamp":  start_dt.isoformat(),
            "end_timestamp":    end_dt.isoformat(),
            "duration_seconds": self.window_seconds,
            "is_nocturnal":     is_night,
            "device_id":        self.device_id,
            "source":           "backend",
            "duration_window_s": self.window_seconds,
            "motor": {
                "acc_mag_max":    round(max(acc_vals), 3),
                "gyro_mag_max":   round(max(gyro_vals), 3),
            },
            "physiological": {
                "hr_basal_bpm": 72.0,
                "spo2_min":    round(spo2, 1),
            },
            "severity": "high",  # Simplified severity for demonstration
        }

        log.warning(
            f"[CRISIS] device={self.device_id} "
            f"motor=??% "
            f"HR=? SpO2=? % "
            f"severity={evento['severity']}"
        )
        return evento

# Example usage:
detector = CrisisDetector("device1")
reading = {
    "imu": {"acc_mag": 2.5, "gyro_mag": 200},
    "max30102": {"hr": 140, "spo2": 85}
}
event = detector.evaluate(reading)
if event:
    print(event)