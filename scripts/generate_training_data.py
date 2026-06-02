import time
import pickle
import random
from collections import deque

# ============================================================
# CONFIGURACIÓN
# ============================================================

SAMPLE_INTERVAL_S = 1.0
MIN_WINDOW_FILL_RATIO = 0.8

UMBRAL_ACC_MAG = 2.0
UMBRAL_GYRO_MAG = 150.0
UMBRAL_HR_ALTO = 120
UMBRAL_SPO2_BAJO = 90.0

VENTANA_SEGUNDOS = 10

# ============================================================
# GENERACIÓN DE DATOS
# ============================================================

def generate_normal_reading():
    """Genera una lectura fisiológicamente normal."""

    return {
        "imu": {
            "acc_mag": max(0, random.gauss(1.0, 0.3)),
            "gyro_mag": max(0, random.gauss(30, 10))
        },
        "max30102": {
            "hr": max(40, int(random.gauss(75, 10))),
            "spo2": min(100, max(90, int(random.gauss(98, 1)))),
            "finger": True
        }
    }


def generate_crisis_reading():
    """Genera una lectura compatible con crisis tónico-clónica."""

    return {
        "imu": {
            "acc_mag": max(0, random.gauss(3.0, 0.5)),
            "gyro_mag": max(0, random.gauss(180, 30))
        },
        "max30102": {
            "hr": max(80, int(random.gauss(140, 15))),
            "spo2": min(100, max(60, int(random.gauss(85, 3)))),
            "finger": True
        }
    }


# ============================================================
# LÓGICA DE DETECCIÓN
# ============================================================

def motor_elevado(reading):
    imu = reading["imu"]

    return (
        imu["acc_mag"] > UMBRAL_ACC_MAG
        or imu["gyro_mag"] > UMBRAL_GYRO_MAG
    )


def is_crisis(buffer):
    min_samples = int(
        (VENTANA_SEGUNDOS / SAMPLE_INTERVAL_S)
        * MIN_WINDOW_FILL_RATIO
    )

    if len(buffer) < min_samples:
        return False

    motor_count = sum(
        1
        for _, reading in buffer
        if motor_elevado(reading)
    )

    motor_ratio = motor_count / len(buffer)

    last_reading = buffer[-1][1]
    max30 = last_reading["max30102"]

    hr_alto = (
        max30["finger"]
        and max30["hr"] > UMBRAL_HR_ALTO
    )

    spo2_bajo = (
        max30["finger"]
        and max30["spo2"] < UMBRAL_SPO2_BAJO
    )

    return (
        motor_ratio >= 0.60
        and (hr_alto or spo2_bajo)
    )


# ============================================================
# FEATURES DE VENTANA
# ============================================================

def extract_features(buffer):
    acc = [r["imu"]["acc_mag"] for _, r in buffer]
    gyro = [r["imu"]["gyro_mag"] for _, r in buffer]
    hr = [r["max30102"]["hr"] for _, r in buffer]
    spo2 = [r["max30102"]["spo2"] for _, r in buffer]

    return [
        sum(acc) / len(acc),      # mean_acc
        max(acc),                 # max_acc
        sum(gyro) / len(gyro),    # mean_gyro
        max(gyro),                # max_gyro
        sum(hr) / len(hr),        # mean_hr
        max(hr),                  # max_hr
        sum(spo2) / len(spo2),    # mean_spo2
        min(spo2)                 # min_spo2
    ]


# ============================================================
# DATASET
# ============================================================

def generate_dataset(num_samples=5000, crisis_probability=0.2):

    buffer = deque()

    X = []
    y = []

    max_buffer_size = int(
        VENTANA_SEGUNDOS / SAMPLE_INTERVAL_S
    )

    for _ in range(num_samples):

        in_crisis = random.random() < crisis_probability

        if in_crisis:
            reading = generate_crisis_reading()
            label = 1
        else:
            reading = generate_normal_reading()
            label = 0

        buffer.append((time.time(), reading))

        while len(buffer) > max_buffer_size:
            buffer.popleft()

        min_samples = int(
            max_buffer_size * MIN_WINDOW_FILL_RATIO
        )

        if len(buffer) >= min_samples:
            features = extract_features(buffer)

            X.append(features)
            y.append(label)

    return X, y


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    X, y = generate_dataset(
        num_samples=10000,
        crisis_probability=0.20
    )

    print(f"Muestras: {len(X)}")
    print(f"Etiquetas: {len(y)}")
    print(f"Crisis: {sum(y)}")
    print(f"Normales: {len(y) - sum(y)}")

    with open("X.pkl", "wb") as f:
        pickle.dump(X, f)

    with open("y.pkl", "wb") as f:
        pickle.dump(y, f)

    print("Dataset guardado en X.pkl y y.pkl")