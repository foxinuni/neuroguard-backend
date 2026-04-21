import logging
import os

import firebase_admin
from firebase_admin import credentials, firestore, messaging

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
        1. Comprueba si el paciente tiene una actividad activa → marca como suprimido.
        2. Guarda el evento en Firestore con el flag 'suppressed' correcto.
        3. Si NO está suprimido, envía notificación push a los cuidadores.
        """
        try:
            # 1. Suprimir si hay actividad activa
            active_activity = self.get_active_activity(patient_id)
            if active_activity and active_activity.get("can_suppress", True):
                event_data["suppressed"] = True
                event_data["activity_context"] = {
                    "label": active_activity.get("type", "unknown"),
                    "confidence": 1.0,
                }
                log.info(
                    f"Evento suprimido por actividad activa: "
                    f"{active_activity.get('type', 'unknown')}"
                )
            else:
                event_data.setdefault("suppressed", False)

            # 2. Guardar en la colección events del paciente
            patient_ref = (
                self.db
                .collection("patients").document(patient_id)
                .collection("events")
            )
            _, doc_ref = patient_ref.add(event_data)
            event_id = doc_ref.id

            # Actualizar resumen en el documento del paciente
            self.db.collection("patients").document(patient_id).set(
                {
                    "last_event_timestamp": event_data.get("timestamp"),
                    "last_event_id": event_id,
                },
                merge=True,
            )

            log.info(
                f"Evento guardado: patients/{patient_id}/events/{event_id} "
                f"(suppressed={event_data['suppressed']})"
            )

            # 3. Notificar a cuidadores solo si el evento no está suprimido
            if not event_data["suppressed"]:
                self.send_fcm_to_caregivers(patient_id, event_data, event_id)

        except Exception as e:
            log.error(f"Error guardando evento: {e}")

    # ──────────────────────────────────────────
    # ACTIVIDADES DEL PACIENTE
    # ──────────────────────────────────────────
    def get_active_activity(self, patient_id: str) -> dict | None:
        """
        Retorna la actividad activa del paciente (end_timestamp == null), o None.
        Se usa para suprimir eventos de crisis mientras el paciente está en
        una actividad registrada (ejercicio, sueño, conducción, etc.).
        """
        try:
            snap = (
                self.db
                .collection("patients").document(patient_id)
                .collection("activities")
                .where("end_timestamp", "==", None)
                .limit(1)
                .get()
            )
            return snap[0].to_dict() if snap else None
        except Exception as e:
            log.error(f"Error leyendo actividad activa de {patient_id}: {e}")
            return None

    def get_patient_location(self, patient_id: str) -> dict | None:
        """
        Retorna el campo 'location' del perfil del paciente en la colección users.
        El campo es escrito por LocationService en la app Flutter.
        Formato: {'latitude': ..., 'longitude': ...}
        """
        try:
            snap = (
                self.db
                .collection("users")
                .where("patient_id", "==", patient_id)
                .where("role", "==", "patient")
                .limit(1)
                .get()
            )
            if not snap:
                return None
            return snap[0].to_dict().get("location")
        except Exception as e:
            log.error(f"Error leyendo ubicación del paciente {patient_id}: {e}")
            return None

    # ──────────────────────────────────────────
    # NOTIFICACIONES FCM A CUIDADORES
    # ──────────────────────────────────────────
    def send_fcm_to_caregivers(self, patient_id: str, event_data: dict, event_id: str) -> None:
        """
        Busca todos los cuidadores vinculados al paciente y les envía una
        notificación push con el detalle de la crisis y la última ubicación
        conocida del paciente.
        """
        try:
            caregivers = (
                self.db
                .collection("users")
                .where("role", "==", "caregiver")
                .where("linked_patient_id", "==", patient_id)
                .get()
            )
            if not caregivers:
                log.info(f"No hay cuidadores vinculados a {patient_id}, FCM omitido")
                return

            location = self.get_patient_location(patient_id)
            severity = event_data.get("severity", "low")

            messages: list[messaging.Message] = []
            for doc in caregivers:
                token = doc.to_dict().get("fcm_token")
                if not token:
                    log.debug(f"Cuidador {doc.id} no tiene FCM token registrado")
                    continue

                data_payload: dict[str, str] = {
                    "type": "crisis_alert",
                    "patient_id": patient_id,
                    "event_id": event_id,
                    "severity": severity,
                    "timestamp": str(event_data.get("timestamp", "")),
                }
                if location:
                    data_payload["lat"] = str(location.get("lat", ""))
                    data_payload["lng"] = str(location.get("lng", ""))

                messages.append(
                    messaging.Message(
                        notification=messaging.Notification(
                            title="⚠️ Crisis detectada",
                            body=(
                                f"Tu paciente está teniendo un episodio ({severity}). "
                                "Abre la app para ver su estado."
                            ),
                        ),
                        data=data_payload,
                        android=messaging.AndroidConfig(priority="high"),
                        token=token,
                    )
                )

            if not messages:
                log.info(f"Ningún cuidador de {patient_id} tiene FCM token, FCM omitido")
                return

            response = messaging.send_each(messages)
            log.info(
                f"FCM enviado: {response.success_count}/{len(messages)} cuidadores "
                f"de {patient_id} notificados"
            )
            for i, r in enumerate(response.responses):
                if not r.success:
                    log.warning(f"  FCM error en cuidador #{i}: {r.exception}")
        except Exception as e:
            log.error(f"Error enviando FCM a cuidadores de {patient_id}: {e}")

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
