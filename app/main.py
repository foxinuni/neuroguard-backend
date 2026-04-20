import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from firebase_client import FirebaseClient
from crisis_detector import CrisisDetector

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("neuroguard")

# ──────────────────────────────────────────────
# CONFIGURACION MQTT (desde variables de entorno)
# ──────────────────────────────────────────────
MQTT_HOST     = os.getenv("MQTT_HOST", "b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER     = os.getenv("MQTT_USER", "Iot2026")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "Qwert1234")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "neuroguard-backend-subscriber")

# Tópicos suscritos (wildcard + para cualquier patient_id y device_id)
TOPIC_TELEMETRY = "neuroguard/+/+/telemetry"
TOPIC_EVENT     = "neuroguard/+/+/event"
TOPIC_STATUS    = "neuroguard/+/+/status"

# ──────────────────────────────────────────────
# ESTADO GLOBAL
# ──────────────────────────────────────────────
firebase   = FirebaseClient()
detectors  = {}   # detector por device_id, se crea on-demand


def get_detector(device_id: str) -> CrisisDetector:
    """Retorna (o crea) el detector de crisis para un dispositivo."""
    if device_id not in detectors:
        detectors[device_id] = CrisisDetector(device_id)
    return detectors[device_id]


# ──────────────────────────────────────────────
# PARSEO DE TÓPICO
# ──────────────────────────────────────────────
def parse_topic(topic: str) -> tuple[str, str, str] | None:
    """
    Extrae (patient_id, device_id, message_type) del tópico.
    Formato esperado: neuroguard/{patient_id}/{device_id}/{type}
    """
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "neuroguard":
        return None
    return parts[1], parts[2], parts[3]


# ──────────────────────────────────────────────
# HANDLERS DE MENSAJES
# ──────────────────────────────────────────────
def handle_telemetry(patient_id: str, device_id: str, payload: dict):
    """
    Procesa un mensaje de telemetría:
    1. Actualiza el documento 'latest' para el dashboard en tiempo real.
    2. Guarda la lectura en el historial (cada N muestras para no saturar).
    3. Corre el detector de crisis y si detecta, genera un evento.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    payload["timestamp"]  = timestamp
    payload["patient_id"] = patient_id
    payload["device_id"]  = device_id

    # 1. Actualizar latest → el dashboard React escucha este documento
    firebase.set_latest_telemetry(patient_id, device_id, payload)

    # 2. Guardar en historial (el cliente decide cada cuántas muestras)
    firebase.add_telemetry_reading(patient_id, device_id, payload)

    # 3. Detección de crisis
    detector = get_detector(device_id)
    crisis = detector.evaluate(payload)

    if crisis:
        log.warning(f"[CRISIS DETECTADA] patient={patient_id} device={device_id} → {crisis}")
        firebase.add_event(patient_id, device_id, crisis)


def handle_event(patient_id: str, device_id: str, payload: dict):
    """
    Procesa un evento enviado directamente por el ESP32.
    (Si en el futuro el ESP32 detecta localmente la crisis.)
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    payload["timestamp"]  = timestamp
    payload["patient_id"] = patient_id
    payload["device_id"]  = device_id
    payload["source"]     = "device"   # a diferencia de "backend" cuando detecta el servidor

    log.warning(f"[EVENTO DISPOSITIVO] patient={patient_id} device={device_id}")
    firebase.add_event(patient_id, device_id, payload)


def handle_status(patient_id: str, device_id: str, payload: dict):
    """Actualiza el estado online/offline del dispositivo en Firestore."""
    payload["last_seen"] = datetime.now(timezone.utc).isoformat()
    firebase.update_device_status(patient_id, device_id, payload)
    log.info(f"[STATUS] {device_id} → {payload.get('status', 'unknown')}")


# ──────────────────────────────────────────────
# CALLBACKS MQTT
# ──────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code.is_failure if hasattr(reason_code, 'is_failure') else reason_code != 0:
        log.error(f"Fallo de conexión MQTT, código: {reason_code}")
        return
    log.info("Conectado al broker MQTT HiveMQ ✓")
    # Suscribirse a todos los tópicos
    client.subscribe(TOPIC_TELEMETRY, qos=1)
    client.subscribe(TOPIC_EVENT,     qos=1)
    client.subscribe(TOPIC_STATUS,    qos=1)
    log.info(f"Suscrito a: {TOPIC_TELEMETRY}, {TOPIC_EVENT}, {TOPIC_STATUS}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    log.warning(f"Desconectado del broker (rc={reason_code}). Reconectando...")


def on_message(client, userdata, msg):
    topic   = msg.topic
    raw     = msg.payload.decode("utf-8", errors="replace")

    parsed = parse_topic(topic)
    if not parsed:
        log.warning(f"Tópico inesperado: {topic}")
        return

    patient_id, device_id, msg_type = parsed

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"JSON inválido en tópico {topic}: {raw[:80]}")
        return

    log.info(f"[{msg_type.upper()}] {patient_id}/{device_id}")

    if msg_type == "telemetry":
        handle_telemetry(patient_id, device_id, payload)
    elif msg_type == "event":
        handle_event(patient_id, device_id, payload)
    elif msg_type == "status":
        handle_status(patient_id, device_id, payload)
    else:
        log.warning(f"Tipo de mensaje desconocido: {msg_type}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    log.info("Iniciando NeuroGuard Backend Subscriber...")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv5,
    )

    # TLS para HiveMQ Cloud (puerto 8883)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.tls_insecure_set(False)   # verificar certificado del servidor
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    # Reconexión automática
    client.reconnect_delay_set(min_delay=2, max_delay=30)

    log.info(f"Conectando a {MQTT_HOST}:{MQTT_PORT}...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    # Bucle bloqueante — el proceso vive aquí para siempre
    client.loop_forever()


if __name__ == "__main__":
    main()
