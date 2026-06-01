#include <Wire.h>
#include <math.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <time.h>
#include "MAX30105.h"
#include "heartRate.h"

// ======================================================
// CONFIGURACION WiFi
// ======================================================
const char* WIFI_SSID     = "iPhone de AndrÃ©s Felipe";
const char* WIFI_PASSWORD = "12345678";

// ======================================================
// CONFIGURACION MQTT - HiveMQ Cloud
// ======================================================
const char* MQTT_HOST      = "b63334e69deb428284644bb4228f807c.s1.eu.hivemq.cloud";
const int   MQTT_PORT      = 8883;
const char* MQTT_USER      = "Iot2026";
const char* MQTT_PASSWORD  = "Qwert1234";
const char* MQTT_CLIENT_ID = "esp32_neuroguard_001";

// Topicos
const char* TOPIC_TELEMETRY = "neuroguard/paciente_001/esp32_001/telemetry";
const char* TOPIC_STATUS    = "neuroguard/paciente_001/esp32_001/status";

// ======================================================
// CONFIGURACION I2C - ESP32 NORMAL
// ======================================================
#define SDA_PIN 21
#define SCL_PIN 22

// ======================================================
// DIRECCIONES GY-85
// ======================================================
#define ADXL345_ADDR 0x53
#define ITG3205_ADDR 0x68

// ======================================================
// CLIENTES WiFi y MQTT
// ======================================================
WiFiClientSecure wifiClient;
PubSubClient mqttClient(wifiClient);

// ======================================================
// OBJETO MAX30102
// ======================================================
MAX30105 particleSensor;

// ======================================================
// OFFSETS IMU
// ======================================================
float offsetAx = 0.0, offsetAy = 0.0, offsetAz = 0.0;
float offsetGx = 0.0, offsetGy = 0.0, offsetGz = 0.0;

// ======================================================
// VARIABLES FILTRADAS IMU
// ======================================================
float axFilt = 0.0, ayFilt = 0.0, azFilt = 0.0;
float gxFilt = 0.0, gyFilt = 0.0, gzFilt = 0.0;

const float alphaAcc  = 0.85;
const float alphaGyro = 0.90;

// ======================================================
// HR - MAX30102
// ======================================================
const byte RATE_SIZE = 8;
byte rates[RATE_SIZE];
byte rateSpot  = 0;
long lastBeat  = 0;

float bpmInstantaneo = 0.0;
int   bpmPromedio    = 0;
float bpmFiltrado    = 0.0;
bool  primerValorBPM = true;

const float BPM_MIN        = 40.0;
const float BPM_MAX        = 200.0;
const float alphaBPM       = 0.8;
const float CAMBIO_MAX_BPM = 6.0;

// ======================================================
// SpO2 - MAX30102
// ======================================================
const int N = 150;
long redBuffer[N];
long irBuffer[N];
int  bufferIndex = 0;
bool bufferLleno = false;

float rFiltrada   = 0.0;
bool  primerR     = true;
const float alphaR = 0.85;

float spo2Filtrada   = 0.0;
bool  primeraSpO2    = true;
const float alphaSpO2 = 0.90;

const long   UMBRAL_DEDO           = 50000;
const double R_MIN_VALIDO          = 0.4;
const double R_MAX_VALIDO          = 1.2;
const double PI_IR_MIN             = 0.0008;
const float  CAMBIO_MAX_POR_UPDATE = 1.0;
const unsigned long SPO2_UPDATE_MS = 300;
unsigned long ultimoUpdateSpO2     = 0;

// ======================================================
// CONTROL DE PUBLICACION MQTT
// ======================================================
const unsigned long MQTT_PUBLISH_INTERVAL = 500;
unsigned long ultimoPublish = 0;

// ======================================================
// FUNCIONES WiFi y MQTT
// ======================================================
void conectarWiFi() {
  Serial.print("Conectando a WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int intentos = 0;
  while (WiFi.status() != WL_CONNECTED && intentos < 20) {
    delay(500);
    Serial.print(".");
    intentos++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi conectado!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nFallo WiFi. Continuando sin conexion...");
  }
}

void conectarMQTT() {
  wifiClient.setInsecure();

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setBufferSize(600);

  Serial.print("Conectando a HiveMQ...");

  int intentos = 0;
  while (!mqttClient.connected() && intentos < 5) {
    if (mqttClient.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASSWORD)) {
      Serial.println(" Conectado!");
      mqttClient.publish(TOPIC_STATUS, "{\"status\":\"online\"}", true);
    } else {
      Serial.print(" Fallo, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" Reintentando en 2s...");
      delay(2000);
      intentos++;
    }
  }
}

void mantenerMQTT() {
  if (!mqttClient.connected()) {
    Serial.println("MQTT desconectado. Reconectando...");
    conectarMQTT();
  }

  mqttClient.loop();
}

// ======================================================
// FUNCIONES I2C GENERALES
// ======================================================
void escribirRegistro(uint8_t direccion, uint8_t registro, uint8_t valor) {
  Wire.beginTransmission(direccion);
  Wire.write(registro);
  Wire.write(valor);
  Wire.endTransmission();
}

// ======================================================
// ADXL345
// ======================================================
void iniciarADXL345() {
  escribirRegistro(ADXL345_ADDR, 0x2D, 0x08);
  escribirRegistro(ADXL345_ADDR, 0x31, 0x08);
  escribirRegistro(ADXL345_ADDR, 0x2C, 0x0A);

  Serial.println("ADXL345 iniciado");
}

void leerADXL345(int16_t &ax, int16_t &ay, int16_t &az) {
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(0x32);
  Wire.endTransmission(false);

  Wire.requestFrom(ADXL345_ADDR, (uint8_t)6);

  if (Wire.available() == 6) {
    uint8_t x0 = Wire.read();
    uint8_t x1 = Wire.read();
    uint8_t y0 = Wire.read();
    uint8_t y1 = Wire.read();
    uint8_t z0 = Wire.read();
    uint8_t z1 = Wire.read();

    ax = (int16_t)((x1 << 8) | x0);
    ay = (int16_t)((y1 << 8) | y0);
    az = (int16_t)((z1 << 8) | z0);
  }
}

// ======================================================
// ITG3205
// ======================================================
void iniciarITG3205() {
  escribirRegistro(ITG3205_ADDR, 0x3E, 0x00);
  delay(100);

  escribirRegistro(ITG3205_ADDR, 0x15, 0x09);
  escribirRegistro(ITG3205_ADDR, 0x16, 0x18);

  Serial.println("ITG3205 iniciado");
}

void leerITG3205(int16_t &gx, int16_t &gy, int16_t &gz, int16_t &tempRaw) {
  Wire.beginTransmission(ITG3205_ADDR);
  Wire.write(0x1B);
  Wire.endTransmission(false);

  Wire.requestFrom(ITG3205_ADDR, (uint8_t)8);

  if (Wire.available() == 8) {
    uint8_t th = Wire.read();
    uint8_t tl = Wire.read();
    uint8_t xh = Wire.read();
    uint8_t xl = Wire.read();
    uint8_t yh = Wire.read();
    uint8_t yl = Wire.read();
    uint8_t zh = Wire.read();
    uint8_t zl = Wire.read();

    tempRaw = (int16_t)((th << 8) | tl);
    gx = (int16_t)((xh << 8) | xl);
    gy = (int16_t)((yh << 8) | yl);
    gz = (int16_t)((zh << 8) | zl);
  }
}

// ======================================================
// CALIBRACION IMU
// ======================================================
void calibrarIMU(int muestras = 1500) {
  long sumaAx = 0, sumaAy = 0, sumaAz = 0;
  long sumaGx = 0, sumaGy = 0, sumaGz = 0;

  Serial.println("Calibrando IMU... deja el GY-85 quieto");
  delay(3000);

  for (int i = 0; i < muestras; i++) {
    int16_t ax = 0, ay = 0, az = 0;
    int16_t gx = 0, gy = 0, gz = 0, tempRaw = 0;

    leerADXL345(ax, ay, az);
    leerITG3205(gx, gy, gz, tempRaw);

    sumaAx += ax;
    sumaAy += ay;
    sumaAz += az;

    sumaGx += gx;
    sumaGy += gy;
    sumaGz += gz;

    delay(3);
  }

  float oneG_raw = 1.0 / 0.0039;

  offsetAx = (float)sumaAx / muestras;
  offsetAy = (float)sumaAy / muestras;
  offsetAz = (float)sumaAz / muestras + oneG_raw;

  offsetGx = (float)sumaGx / muestras;
  offsetGy = (float)sumaGy / muestras;
  offsetGz = (float)sumaGz / muestras;

  Serial.println("Calibracion IMU terminada");

  Serial.println("Offsets calculados:");
  Serial.print("offsetAx: "); Serial.println(offsetAx);
  Serial.print("offsetAy: "); Serial.println(offsetAy);
  Serial.print("offsetAz: "); Serial.println(offsetAz);
  Serial.print("offsetGx: "); Serial.println(offsetGx);
  Serial.print("offsetGy: "); Serial.println(offsetGy);
  Serial.print("offsetGz: "); Serial.println(offsetGz);
}

// ======================================================
// REINICIAR ESTADO DEL MAX30102
// ======================================================
void reiniciarEstadoMAX() {
  bpmInstantaneo = 0.0;
  bpmPromedio = 0;
  bpmFiltrado = 0.0;
  primerValorBPM = true;

  rateSpot = 0;
  for (int i = 0; i < RATE_SIZE; i++) {
    rates[i] = 0;
  }

  bufferIndex = 0;
  bufferLleno = false;

  rFiltrada = 0.0;
  primerR = true;

  spo2Filtrada = 0.0;
  primeraSpO2 = true;
}

// ======================================================
// NTP - SINCRONIZACION DE TIEMPO
// ======================================================
void sincronizarNTP() {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print("Sincronizando NTP");
  unsigned long start = millis();
  while (time(nullptr) < 1000000000UL && millis() - start < 10000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(time(nullptr) > 1000000000UL ? " OK" : " timeout (usando hora local)");
}

void obtenerTimestampISO(char* buf, size_t len) {
  time_t now = time(nullptr);
  if (now < 1000000000UL) { buf[0] = '\0'; return; }
  struct tm* t = gmtime(&now);
  strftime(buf, len, "%Y-%m-%dT%H:%M:%SZ", t);
}

// ======================================================
// SETUP
// ======================================================
void setup() {
  Serial.begin(115200);
  delay(1000);

  Wire.begin(SDA_PIN, SCL_PIN);

  // ---------------- IMU ----------------
  Serial.println("Iniciando GY-85...");
  iniciarADXL345();
  iniciarITG3205();
  calibrarIMU();

  // ---------------- MAX30102 ----------------
  Serial.println("Iniciando MAX30102...");

  if (!particleSensor.begin(Wire, I2C_SPEED_STANDARD)) {
    Serial.println("No se encontro el MAX30102. Revisa conexiones.");
    while (1);
  }

  Serial.println("MAX30102 detectado.");

  particleSensor.setup();
  particleSensor.setPulseAmplitudeRed(0x1F);
  particleSensor.setPulseAmplitudeIR(0x1F);
  particleSensor.setPulseAmplitudeGreen(0);

  conectarWiFi();

  if (WiFi.status() == WL_CONNECTED) {
    sincronizarNTP();
    conectarMQTT();
  }

  Serial.println("Sistema listo.");
}

// ======================================================
// LOOP
// ======================================================
void loop() {
  // ==================================================
  // IMU
  // ==================================================
  int16_t axRaw = 0, ayRaw = 0, azRaw = 0;
  int16_t gxRaw = 0, gyRaw = 0, gzRaw = 0, tempRaw = 0;

  leerADXL345(axRaw, ayRaw, azRaw);
  leerITG3205(gxRaw, gyRaw, gzRaw, tempRaw);

  float axCorr = (axRaw - offsetAx) * 0.0039;
  float ayCorr = (ayRaw - offsetAy) * 0.0039;
  float azCorr = (azRaw - offsetAz) * 0.0039;

  float gxCorr = (gxRaw - offsetGx) / 14.375;
  float gyCorr = (gyRaw - offsetGy) / 14.375;
  float gzCorr = (gzRaw - offsetGz) / 14.375;

  axFilt = alphaAcc * axFilt + (1.0 - alphaAcc) * axCorr;
  ayFilt = alphaAcc * ayFilt + (1.0 - alphaAcc) * ayCorr;
  azFilt = alphaAcc * azFilt + (1.0 - alphaAcc) * azCorr;

  gxFilt = alphaGyro * gxFilt + (1.0 - alphaGyro) * gxCorr;
  gyFilt = alphaGyro * gyFilt + (1.0 - alphaGyro) * gyCorr;
  gzFilt = alphaGyro * gzFilt + (1.0 - alphaGyro) * gzCorr;

  float accMag  = sqrt(axFilt * axFilt + ayFilt * ayFilt + azFilt * azFilt);
  float gyroMag = sqrt(gxFilt * gxFilt + gyFilt * gyFilt + gzFilt * gzFilt);

  // ==================================================
  // MAX30102
  // ==================================================
  long irValue = particleSensor.getIR();
  long redValue = particleSensor.getRed();

  bool dedoDetectado = true;

  if (irValue < UMBRAL_DEDO) {
    dedoDetectado = false;
    reiniciarEstadoMAX();
  } else {
    dedoDetectado = true;

    // ---------------- HR ----------------
    if (checkForBeat(irValue) == true) {
      long delta = millis() - lastBeat;
      lastBeat = millis();

      bpmInstantaneo = 60.0 / (delta / 1000.0);

      if (bpmInstantaneo > BPM_MIN && bpmInstantaneo < BPM_MAX) {
        rates[rateSpot++] = (byte)bpmInstantaneo;
        rateSpot %= RATE_SIZE;

        int suma = 0;
        int cantidad = 0;

        for (byte i = 0; i < RATE_SIZE; i++) {
          if (rates[i] > 0) {
            suma += rates[i];
            cantidad++;
          }
        }

        if (cantidad > 0) {
          bpmPromedio = suma / cantidad;
        }

        if (primerValorBPM) {
          bpmFiltrado = bpmInstantaneo;
          primerValorBPM = false;
        } else {
          float bpmObjetivo = bpmInstantaneo;

          if (bpmObjetivo > bpmFiltrado + CAMBIO_MAX_BPM) {
            bpmObjetivo = bpmFiltrado + CAMBIO_MAX_BPM;
          } else if (bpmObjetivo < bpmFiltrado - CAMBIO_MAX_BPM) {
            bpmObjetivo = bpmFiltrado - CAMBIO_MAX_BPM;
          }

          bpmFiltrado = alphaBPM * bpmFiltrado + (1.0 - alphaBPM) * bpmObjetivo;
        }
      }
    }

    // ---------------- SpO2 ----------------
    redBuffer[bufferIndex] = redValue;
    irBuffer[bufferIndex] = irValue;

    bufferIndex++;

    if (bufferIndex >= N) {
      bufferIndex = 0;
      bufferLleno = true;
    }

    if (bufferLleno && millis() - ultimoUpdateSpO2 >= SPO2_UPDATE_MS) {
      ultimoUpdateSpO2 = millis();

      double dcRed = 0.0;
      double dcIR  = 0.0;

      for (int i = 0; i < N; i++) {
        dcRed += redBuffer[i];
        dcIR  += irBuffer[i];
      }

      dcRed /= N;
      dcIR  /= N;

      double sumaCuadRed = 0.0;
      double sumaCuadIR  = 0.0;

      for (int i = 0; i < N; i++) {
        double redAC = redBuffer[i] - dcRed;
        double irAC  = irBuffer[i] - dcIR;

        sumaCuadRed += redAC * redAC;
        sumaCuadIR  += irAC * irAC;
      }

      double acRed = sqrt(sumaCuadRed / N);
      double acIR  = sqrt(sumaCuadIR / N);

      double piIR = acIR / dcIR;

      if (dcRed > 0 && dcIR > 0 && acIR > 0 && piIR > PI_IR_MIN) {
        double R = (acRed / dcRed) / (acIR / dcIR);

        if (R >= R_MIN_VALIDO && R <= R_MAX_VALIDO) {
          if (primerR) {
            rFiltrada = R;
            primerR = false;
          } else {
            rFiltrada = alphaR * rFiltrada + (1.0 - alphaR) * R;
          }

          float spo2Calculada = 110.0 - 25.0 * rFiltrada;

          if (spo2Calculada > 100.0) spo2Calculada = 100.0;
          if (spo2Calculada < 70.0)  spo2Calculada = 70.0;

          if (primeraSpO2) {
            spo2Filtrada = spo2Calculada;
            primeraSpO2 = false;
          } else {
            float spo2Objetivo = spo2Calculada;

            if (spo2Objetivo > spo2Filtrada + CAMBIO_MAX_POR_UPDATE) {
              spo2Objetivo = spo2Filtrada + CAMBIO_MAX_POR_UPDATE;
            } else if (spo2Objetivo < spo2Filtrada - CAMBIO_MAX_POR_UPDATE) {
              spo2Objetivo = spo2Filtrada - CAMBIO_MAX_POR_UPDATE;
            }

            spo2Filtrada = alphaSpO2 * spo2Filtrada + (1.0 - alphaSpO2) * spo2Objetivo;
          }
        }
      }
    }
  }

  // ==================================================
  // JSON IMU + MAX30102
  // ==================================================
  char tsBuffer[26];
  obtenerTimestampISO(tsBuffer, sizeof(tsBuffer));

  char jsonBuffer[560];

  snprintf(
    jsonBuffer,
    sizeof(jsonBuffer),
    "{\"imu\":{\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f,\"acc_mag\":%.3f,\"gx\":%.3f,\"gy\":%.3f,\"gz\":%.3f,\"gyro_mag\":%.3f},\"max30102\":{\"ir\":%ld,\"red\":%ld,\"hr\":%.1f,\"spo2\":%.1f,\"finger\":%s},\"timestamp\":\"%s\"}",
    axFilt, ayFilt, azFilt, accMag,
    gxFilt, gyFilt, gzFilt, gyroMag,
    irValue,
    redValue,
    bpmFiltrado,
    spo2Filtrada,
    dedoDetectado ? "true" : "false",
    tsBuffer
  );

  Serial.println(jsonBuffer);

  // publicar MQTT IMU + MAX30102
  if (WiFi.status() == WL_CONNECTED) {
    mantenerMQTT();

    if (millis() - ultimoPublish >= MQTT_PUBLISH_INTERVAL) {
      ultimoPublish = millis();
      mqttClient.publish(TOPIC_TELEMETRY, jsonBuffer);
    }
  }

  delay(20);
}