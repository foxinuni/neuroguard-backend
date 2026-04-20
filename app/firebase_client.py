import logging
import os

import firebase_admin
from firebase_admin import credentials, firestore

log = logging.getLogger("neuroguard.firebase")

# ──────────────────────────────────────────────
# ESTRUCTURA DE FIRESTORE
#
# devices/
#   {device_id}/
#     status, last_seen, patient_id
#
# patients/
#   {patient_id}/
#     devices/
#       {device_id}/
#         latest/          ← documento único, se sobreescribe → dashboard tiempo real
#           ...telemetry
#         readings/        ← colección de lecturas históricas
#           {auto_id}: {...}
#     events/              ← crisis detectadas del paciente
#       {auto_id}: {...}
# ──────────────────────────────────────────────

class FirebaseClient:
    def __init__(self):
        cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "/app/firebase-credentials.json")

        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            log.info("Firebase Admin SDK inicializado ✓")

        self.db = firestore.client()

        # Contador para submuestreo del historial
        # Solo guarda 1 de cada N lecturas para no saturar Firestore
        self._reading_counter: dict[str, int] = {}
        self.HISTORY_SUBSAMPLE = int(os.getenv("HISTORY_SUBSAMPLE", "5"))

    # ──────────────────────────────────────────
    # TELEMETRÍA
    # ──────────────────────────────────────────
    def set_latest_telemetry(self, patient_id: str, device_id: str, data: dict):
        """
        Sobreescribe el documento 'latest' del dispositivo.
        El dashboard React escucha este documento con onSnapshot()
        y se actualiza automáticamente con cada nueva lectura.
        """
        try:
            ref = (
                self.db
                .collection("patients").document(patient_id)
                .collection("devices").document(device_id)
                .collection("latest").document("current")
            )
            ref.set(data)
        except Exception as e:
            log.error(f"Error guardando latest telemetry: {e}")

    def add_telemetry_reading(self, patient_id: str, device_id: str, data: dict):
        """
        Guarda una lectura en el historial con submuestreo.
        Solo persiste 1 de cada HISTORY_SUBSAMPLE lecturas para controlar costos.
        A 500ms por lectura y submuestreo=5 → una lectura histórica cada 2.5s.
        """
        key = f"{patient_id}_{device_id}"
        self._reading_counter[key] = self._reading_counter.get(key, 0) + 1

        if self._reading_counter[key] % self.HISTORY_SUBSAMPLE != 0:
            return   # no guardar esta muestra

        try:
            ref = (
                self.db
                .collection("patients").document(patient_id)
                .collection("devices").document(device_id)
                .collection("readings")
            )
            ref.add(data)
        except Exception as e:
            log.error(f"Error guardando lectura histórica: {e}")

    # ──────────────────────────────────────────
    # EVENTOS / CRISIS
    # ──────────────────────────────────────────
    def add_event(self, patient_id: str, device_id: str, event_data: dict):
        """
        Guarda un evento de crisis detectado.
        Se guarda en la colección events del paciente para el dashboard.
        """
        try:
            # En la colección del paciente (para el dashboard médico)
            patient_ref = (
                self.db
                .collection("patients").document(patient_id)
                .collection("events")
            )
            _, doc_ref = patient_ref.add(event_data)
            event_id = doc_ref.id

            # También actualizamos un campo de resumen en el paciente
            self.db.collection("patients").document(patient_id).set(
                {
                    "last_event_timestamp": event_data.get("timestamp"),
                    "last_event_id": event_id,
                },
                merge=True,
            )

            log.info(f"Evento guardado: patients/{patient_id}/events/{event_id}")
        except Exception as e:
            log.error(f"Error guardando evento: {e}")

    # ──────────────────────────────────────────
    # ESTADO DEL DISPOSITIVO
    # ──────────────────────────────────────────
    def update_device_status(self, patient_id: str, device_id: str, status_data: dict):
        """Actualiza el estado online/offline y last_seen del dispositivo."""
        try:
            # En la colección global de dispositivos
            self.db.collection("devices").document(device_id).set(
                {**status_data, "patient_id": patient_id},
                merge=True,
            )
        except Exception as e:
            log.error(f"Error actualizando estado del dispositivo: {e}")
