import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

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
VENTANA_SEGUNDOS = 10     # segundos de actividad elevada para confirmar crisis
COOLDOWN_SEGUNDOS = 60    # segundos mínimos entre dos alertas del mismo dispositivo


class CrisisDetector:
    """
    Detector de posibles crisis tónico-clónicas basado en ventana temporal.
    
    Estrategia multimodal:
    1. Analiza la ventana de los últimos N segundos de lecturas.
    2. Si más del 60% de las muestras superan los umbrales motores (acc + gyro),
       Y se confirma con señal fisiológica (HR alto o SpO2 bajo),
       → se genera un evento de posible crisis.
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

        # Necesitamos suficientes muestras para evaluar
        # A 500ms/muestra → 10s de ventana = ~20 muestras esperadas
        if len(self.buffer) < 10:
            return None

        # ── Evaluar actividad motora ──────────────────────
        muestras_motor_alto = sum(
            1 for _, r in self.buffer
            if self._motor_elevado(r)
        )
        porcentaje_motor = muestras_motor_alto / len(self.buffer)

        # ── Evaluar señales fisiológicas ──────────────────
        ultima_lectura = reading
        max30 = ultima_lectura.get("max30102", {})
        hr    = max30.get("hr", 0)
        spo2  = max30.get("spo2", 100)
        finger = max30.get("finger", False)

        hr_alto   = finger and hr > UMBRAL_HR_ALTO
        spo2_bajo = finger and spo2 < UMBRAL_SPO2_BAJO

        # ── Criterio de crisis ────────────────────────────
        # Motor sostenido (>60% de la ventana) + al menos una señal fisiológica
        crisis_detectada = (
            porcentaje_motor >= 0.60
            and (hr_alto or spo2_bajo)
        )

        if not crisis_detectada:
            return None

        # ── Cooldown: no repetir alerta del mismo evento ──
        if (now - self.last_alert_ts) < COOLDOWN_SEGUNDOS:
            return None

        self.last_alert_ts = now

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
                "pct_elevated":   round(porcentaje_motor * 100, 1),
                "acc_mag_max":    round(max(acc_vals), 3),
                "acc_mag_mean":   round(sum(acc_vals) / len(acc_vals), 3),
                "gyro_mag_max":   round(max(gyro_vals), 3),
                "gyro_mag_mean":  round(sum(gyro_vals) / len(gyro_vals), 3),
            },
            "physiological": {
                "hr_basal_bpm": 72.0,
                "hr_peak_bpm": round(hr, 1),
                "spo2_min":    round(spo2, 1),
                "hr_elevated": hr_alto,
                "spo2_low":    spo2_bajo,
            },
            "severity": self._calcular_severidad(porcentaje_motor, hr, spo2),
        }

        log.warning(
            f"[CRISIS] device={self.device_id} "
            f"motor={porcentaje_motor*100:.0f}% "
            f"HR={hr:.0f} SpO2={spo2:.0f}% "
            f"severity={evento['severity']}"
        )
        return evento

    def _motor_elevado(self, reading: dict) -> bool:
        """True si la lectura muestra actividad motora por encima del umbral."""
        imu = reading.get("imu", {})
        acc_mag  = imu.get("acc_mag",  0)
        gyro_mag = imu.get("gyro_mag", 0)
        return acc_mag > UMBRAL_ACC_MAG or gyro_mag > UMBRAL_GYRO_MAG

    def _calcular_severidad(self, pct_motor: float, hr: float, spo2: float) -> str:
        """Clasificación de severidad: low / medium / high."""
        score = 0
        if pct_motor >= 0.80: score += 2
        elif pct_motor >= 0.60: score += 1
        if hr > 140:    score += 2
        elif hr > 120:  score += 1
        if spo2 < 85:   score += 2
        elif spo2 < 90: score += 1

        if score >= 4: return "high"
        if score >= 2: return "medium"
        return "low"
