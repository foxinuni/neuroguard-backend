# NeuroGuard — Backend Subscriber

Proceso Python que se suscribe al broker HiveMQ Cloud, recibe los datos
del ESP32 y los persiste en Firebase Firestore. Corre en Docker 24/7.

---

## Estructura del proyecto

```
neuroguard-backend/
├── app/
│   ├── main.py             ← punto de entrada, callbacks MQTT
│   ├── firebase_client.py  ← escritura en Firestore
│   └── crisis_detector.py  ← detección de posible crisis
├── .env.example            ← plantilla de variables de entorno
├── .gitignore
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## Estructura de Firestore generada

```
patients/
  {patient_id}/
    devices/
      {device_id}/
        latest/
          current          ← documento único, listener tiempo real para el dashboard
        readings/
          {auto_id}        ← historial submuestreado (1 cada 2.5s por defecto)
    events/
      {auto_id}            ← crisis detectadas, con severidad y métricas

devices/
  {device_id}              ← estado online/offline del dispositivo
```

---

## Configuración antes del primer despliegue

### 1. Firebase — obtener credenciales

1. Ir a [Firebase Console](https://console.firebase.google.com/)
2. Crear proyecto `neuroguard` (o usar uno existente)
3. Ir a **Configuración del proyecto → Cuentas de servicio**
4. Clic en **Generar nueva clave privada** → descarga `firebase-credentials.json`
5. Colocar ese archivo en la raíz del proyecto (al lado de `docker-compose.yml`)

### 2. Variables de entorno

```bash
cp .env.example .env
# El .env ya viene pre-rellenado con las credenciales HiveMQ del proyecto
```

### 3. Tópicos del ESP32

Asegúrate de que el ESP32 publique con esta estructura de tópico:
```
neuroguard/{patient_id}/{device_id}/telemetry
neuroguard/{patient_id}/{device_id}/status
neuroguard/{patient_id}/{device_id}/event
```

Ejemplo concreto:
```
neuroguard/paciente_laura/esp32_001/telemetry
```

---

## Despliegue en tu VM

```bash
# 1. Clonar / copiar el proyecto en tu VM
scp -r neuroguard-backend/ usuario@tu-vm:/opt/neuroguard/

# 2. Entrar al directorio
cd /opt/neuroguard/neuroguard-backend

# 3. Colocar firebase-credentials.json aquí

# 4. Copiar .env
cp .env.example .env

# 5. Construir y arrancar
docker compose up -d --build

# 6. Ver logs en tiempo real
docker compose logs -f
```

El servicio tiene `restart: unless-stopped`, así que sobrevive reinicios de la VM.

---

## Comandos útiles

```bash
# Ver estado
docker compose ps

# Ver logs
docker compose logs -f

# Reiniciar
docker compose restart

# Parar
docker compose down

# Reconstruir tras cambios de código
docker compose up -d --build
```

---

## Tópicos MQTT suscritos

| Tópico | Descripción |
|--------|-------------|
| `neuroguard/+/+/telemetry` | Datos continuos IMU + MAX30102 del ESP32 |
| `neuroguard/+/+/event` | Evento enviado directamente por el dispositivo |
| `neuroguard/+/+/status` | Estado online/offline del dispositivo |

---

## Cómo conectar el dashboard React

En tu componente React, escucha el documento `latest/current` del paciente:

```javascript
import { doc, onSnapshot } from "firebase/firestore";

const ref = doc(db,
  "patients", patientId,
  "devices", deviceId,
  "latest", "current"
);

const unsubscribe = onSnapshot(ref, (snap) => {
  if (snap.exists()) {
    const data = snap.data();
    setHeartRate(data.max30102.hr);
    setSpo2(data.max30102.spo2);
    setAccMag(data.imu.acc_mag);
    // etc.
  }
});
```

Para el historial (gráfica de tendencia):

```javascript
import { collection, query, orderBy, limit, onSnapshot } from "firebase/firestore";

const q = query(
  collection(db, "patients", patientId, "devices", deviceId, "readings"),
  orderBy("timestamp", "desc"),
  limit(100)
);
onSnapshot(q, (snap) => {
  const readings = snap.docs.map(d => d.data());
  setChartData(readings.reverse());
});
```
