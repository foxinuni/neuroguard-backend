# NeuroGuard — Contexto del Proyecto para el Agente

> Este archivo existe para que el agente Claude que opera en esta carpeta entienda
> el proyecto completo: qué hay hecho, cómo se comunican las partes y qué sigue.

---

## 1. ¿Qué es NeuroGuard?

Solución IoT para monitoreo continuo de pacientes con epilepsia, orientada a detectar
eventos compatibles con crisis tónico-clónicas. El sistema captura señales fisiológicas
y de movimiento, las transmite a la nube y las visualiza en un dashboard clínico.

**Usuarios objetivo:** pacientes con epilepsia, cuidadores/familiares, neurólogos.

---

## 2. Arquitectura general (3 capas IoT)

```
┌─────────────────────┐
│  CAPA PERCEPCIÓN    │  ESP32 + sensores IMU y MAX30102
│  (Edge)             │  → publica JSON por MQTT/TLS
└────────┬────────────┘
         │ WiFi · MQTT/TLS · puerto 8883
         ▼
┌─────────────────────┐
│  BROKER MQTT        │  HiveMQ Cloud (gratuito)
│  (Red-Transporte)   │  b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud
└────────┬────────────┘
         │ suscripción MQTT
         ▼
┌─────────────────────┐
│  BACKEND SUBSCRIBER │  ← ESTÁS AQUÍ (esta carpeta)
│  (Aplicación)       │  Python · paho-mqtt · firebase-admin
└────────┬────────────┘
         │ HTTPS · Firebase SDK
         ▼
┌─────────────────────┐
│  Firebase Firestore │  Base de datos en la nube
└────────┬────────────┘
         │ onSnapshot() listeners
         ▼
┌─────────────────────┐
│  Dashboard Web      │  React — AÚN NO IMPLEMENTADO
│  App Móvil          │  → siguiente paso del proyecto
└─────────────────────┘
```

---

## 3. Hardware — Sensores del ESP32

El wearable corre en un **ESP32** (no Raspberry Pi, aunque el PDF del proyecto
la menciona — se cambió durante la implementación). Conectado por I²C:

| Sensor | Función | Dirección I²C |
|--------|---------|---------------|
| ADXL345 (parte del GY-85) | Acelerómetro 3 ejes | 0x53 |
| ITG3205 (parte del GY-85) | Giroscopio 3 ejes | 0x68 |
| MAX30102 | PPG → HR + SpO2 | dirección por defecto |

**Variables monitoreadas:**
- `ax, ay, az` — aceleración en g (filtro EMA α=0.85)
- `acc_mag` — magnitud del vector de aceleración
- `gx, gy, gz` — velocidad angular en °/s (filtro EMA α=0.90)
- `gyro_mag` — magnitud del vector giroscópico
- `hr` — frecuencia cardíaca en BPM (filtrado, rango válido 40–200)
- `spo2` — saturación de oxígeno % (fórmula: 110 − 25·R)
- `finger` — bool, si el dedo está en contacto con el MAX30102
- `ir`, `red` — lecturas raw del sensor óptico

---

## 4. Protocolo MQTT — Estructura de tópicos

```
neuroguard/{patient_id}/{device_id}/{type}
```

**Tópicos activos actualmente:**

| Tópico | Dirección | Descripción |
|--------|-----------|-------------|
| `neuroguard/paciente_001/esp32_001/telemetry` | ESP32 → broker | Datos continuos, cada 500ms |
| `neuroguard/paciente_001/esp32_001/status` | ESP32 → broker | Estado online (retained) |
| `neuroguard/paciente_001/esp32_001/event` | ESP32 → broker | Crisis detectada por el dispositivo (futuro) |

**El backend usa wildcards:**
```
neuroguard/+/+/telemetry   ← captura cualquier paciente y dispositivo
neuroguard/+/+/status
neuroguard/+/+/event
```

**Payload de telemetría (JSON publicado por el ESP32):**
```json
{
  "device": "esp32_001",
  "imu": {
    "ax": 0.012,
    "ay": -0.005,
    "az": 0.998,
    "acc_mag": 0.999,
    "gx": 0.139,
    "gy": -0.210,
    "gz": 0.000,
    "gyro_mag": 0.249
  },
  "max30102": {
    "ir": 124500,
    "red": 98200,
    "hr": 72.45,
    "spo2": 97.10,
    "finger": true
  }
}
```

---

## 5. Credenciales y configuración

### MQTT — HiveMQ Cloud
```
Host:     b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud
Puerto:   8883 (TLS)
WS port:  8884
Usuario:  Iot2026
Clave:    Qwert1234
```

### Firebase
- El archivo `firebase-credentials.json` **ya existe** en la raíz del proyecto.
- El proyecto Firebase **ya está creado**.
- El archivo `.env` **ya existe** (copiado desde `.env.example`).

### Variables de entorno (`.env`)
```
MQTT_HOST=b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud
MQTT_PORT=8883
MQTT_USER=Iot2026
MQTT_PASSWORD=Qwert1234
MQTT_CLIENT_ID=neuroguard-backend-01
HISTORY_SUBSAMPLE=5
```

---

## 6. Estructura de esta carpeta (backend)

```
neuroguard-backend/
├── app/
│   ├── main.py              ← entrada: conexión MQTT, callbacks, routing
│   ├── firebase_client.py   ← escritura en Firestore (latest + historial + eventos)
│   └── crisis_detector.py   ← detección de posible crisis con ventana deslizante
├── firebase-credentials.json  ← credenciales Firebase (NO subir a git)
├── .env                       ← variables de entorno (NO subir a git)
├── .env.example               ← plantilla de referencia (sí subir a git)
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── CONTEXT.md                 ← este archivo
```

---

## 7. Estructura de Firestore (generada por el backend)

```
patients/
  {patient_id}/                        ← ej: "paciente_001"
    last_event_timestamp               ← campo en el documento del paciente
    last_event_id
    devices/
      {device_id}/                     ← ej: "esp32_001"
        latest/
          current                      ← documento único, sobreescrito con cada lectura
                                          El dashboard React hace onSnapshot() aquí
                                          para tiempo real
        readings/
          {auto_id}                    ← historial submuestreado (1 de cada 5 lecturas)
                                          ≈ una lectura histórica cada 2.5 segundos
    events/
      {auto_id}                        ← crisis detectadas, con severidad y métricas

devices/
  {device_id}                          ← estado online/offline del dispositivo
```

---

## 8. Lógica de detección de crisis (crisis_detector.py)

Ventana deslizante de **10 segundos** por dispositivo. Genera un evento si:

- **≥ 60%** de las muestras de la ventana superan umbrales motores:
  - `acc_mag > 2.0 g` **O** `gyro_mag > 150 °/s`
- **Y** al menos una señal fisiológica confirma:
  - `hr > 120 bpm` **O** `spo2 < 90%`
- **Y** han pasado al menos **60 segundos** desde la última alerta (cooldown)

Severidad del evento: `low / medium / high` según combinación de umbrales superados.

---

## 9. Despliegue

El backend corre en Docker 24/7 en una VM del equipo.

```bash
# Construir y arrancar
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f

# Reiniciar tras cambios
docker compose up -d --build
```

`restart: unless-stopped` garantiza que el contenedor sobrevive reinicios de la VM.

---

## 10. Qué sigue — Dashboard Web (PRÓXIMO PASO)

El dashboard es una aplicación **React** que consume Firebase Firestore directamente
desde el navegador usando el SDK de Firebase Web.

### Vistas requeridas según el diseño del proyecto:

**Dashboard médico (web):**
- Resumen del paciente (nombre, tipo de epilepsia)
- Última crisis: hace cuánto, duración, tipo
- Métricas del último evento: HR pico, SpO2 mínima, actividad motora (g RMS)
- Gráficas de actividad motora y FC/SpO2 en las fases pre/evento/recuperación
- Tendencia de crisis por semana (barras) y distribución (nocturnas vs diurnas)

**App móvil (secundaria):**
- Home con último evento y eventos recientes por semana
- Historial con ejercicio, medicación, posibles crisis
- Registro de actividades (para reducir falsos positivos)

### Cómo conectar React a Firestore para tiempo real:

```javascript
// Lectura en tiempo real del estado actual del paciente
import { doc, onSnapshot } from "firebase/firestore";

const ref = doc(db,
  "patients", "paciente_001",
  "devices", "esp32_001",
  "latest", "current"
);
const unsubscribe = onSnapshot(ref, (snap) => {
  if (snap.exists()) {
    const data = snap.data();
    setHeartRate(data.max30102.hr);
    setSpo2(data.max30102.spo2);
    setAccMag(data.imu.acc_mag);
    setFinger(data.max30102.finger);
  }
});

// Historial para gráficas
import { collection, query, orderBy, limit, onSnapshot } from "firebase/firestore";

const q = query(
  collection(db, "patients", "paciente_001", "devices", "esp32_001", "readings"),
  orderBy("timestamp", "desc"),
  limit(120)   // últimos 5 minutos aprox. (1 lectura cada 2.5s)
);
onSnapshot(q, (snap) => {
  const readings = snap.docs.map(d => d.data()).reverse();
  setChartData(readings);
});

// Eventos / crisis
const eventsQuery = query(
  collection(db, "patients", "paciente_001", "events"),
  orderBy("timestamp", "desc"),
  limit(20)
);
onSnapshot(eventsQuery, (snap) => {
  setEvents(snap.docs.map(d => ({ id: d.id, ...d.data() })));
});
```

### Stack recomendado para el dashboard:
- **React + Vite** — setup rápido
- **Firebase Web SDK v9+** — conexión a Firestore
- **Recharts** — gráficas de HR, SpO2, actividad motora
- **Tailwind CSS** — estilos (el mockup del proyecto usa un estilo limpio y clínico)

---

## 11. Estado actual del proyecto (resumen)

| Componente | Estado |
|------------|--------|
| Sensores ESP32 (IMU + MAX30102) | ✅ Implementado y funcionando |
| Código ESP32 con WiFi + MQTT | ✅ Listo para prueba con hardware |
| Broker MQTT (HiveMQ Cloud) | ✅ Desplegado y configurado |
| Backend Subscriber (Python) | ✅ Implementado, listo para desplegar |
| Firebase (proyecto + credenciales) | ✅ Creado y configurado |
| Docker / docker-compose | ✅ Listo para levantar |
| Dashboard Web (React) | ⏳ Pendiente — siguiente paso |
| App Móvil | ⏳ Pendiente — fase posterior |
