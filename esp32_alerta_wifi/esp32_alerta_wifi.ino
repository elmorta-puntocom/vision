#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include "mbedtls/md.h"
#include <time.h>

const char* SSID = "placa";
const char* PASSWORD = "12345678";

// Cambia la IP por la del servidor Flask/XAMPP en tu red.
const char* SERVER_BASE_URL = "http://192.168.2.119:5050";

// Estos valores salen de: python scripts/create_device.py
const char* DEVICE_ID = "ESP32-657593";
const char* DEVICE_SECRET = "EAPDHnC5UMsbCZZwMipEqjjz77H0aW3cb9s-xIB9CUA";
const char* FIRMWARE_VERSION = "1.0.0";

const int PIN_MOTOR = 23;
const int PIN_BUZZER = 22;

const unsigned long HEARTBEAT_INTERVAL_MS = 30000;
const unsigned long COMMAND_INTERVAL_MS = 5000;

WebServer server(80);

bool alarmaActiva = false;
unsigned long lastHeartbeat = 0;
unsigned long lastCommandPoll = 0;

String hmacSha256(String message, String key) {
  byte hmac[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_type_t md_type = MBEDTLS_MD_SHA256;

  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(md_type), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char*)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, hmac);
  mbedtls_md_free(&ctx);

  String result = "";
  for (int i = 0; i < 32; i++) {
    char hex[3];
    sprintf(hex, "%02x", hmac[i]);
    result += hex;
  }
  return result;
}

String nonce() {
  return String(random(100000, 999999)) + String(millis());
}

String unixTimestamp() {
  time_t now;
  time(&now);
  return String((long)now);
}

void aplicarAlarma(bool activar) {
  alarmaActiva = activar;

  digitalWrite(PIN_MOTOR, activar ? HIGH : LOW);
  digitalWrite(PIN_BUZZER, activar ? HIGH : LOW);

  Serial.println(activar ? "Alarma ACTIVADA" : "Alarma DESACTIVADA");
}

void responderEstado() {
  String estado = alarmaActiva ? "ON" : "OFF";
  server.send(200, "text/plain", "ESP32 OK - Alarma: " + estado);
}

void alertaOn() {
  aplicarAlarma(true);
  server.send(200, "text/plain", "Alerta ACTIVADA");
}

void alertaOff() {
  aplicarAlarma(false);
  server.send(200, "text/plain", "Alerta DESACTIVADA");
}

void rutaNoEncontrada() {
  server.send(404, "text/plain", "Ruta no encontrada");
}

bool postJson(String endpoint, String body, String &response) {
  HTTPClient http;
  String url = String(SERVER_BASE_URL) + endpoint;

  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  response = http.getString();
  http.end();

  Serial.printf("[API] POST %s -> %d\n", endpoint.c_str(), code);
  if (response.length() > 0) {
    Serial.println(response);
  }

  return code >= 200 && code < 300;
}

bool enviarHeartbeat() {
  String mac = WiFi.macAddress();
  String ts = unixTimestamp();
  String n = nonce();
  String payload = String(DEVICE_ID) + "|" + mac + "|" + ts + "|" + n;
  String signature = hmacSha256(payload, DEVICE_SECRET);

  String body = "{";
  body += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  body += "\"mac\":\"" + mac + "\",";
  body += "\"ts\":\"" + ts + "\",";
  body += "\"nonce\":\"" + n + "\",";
  body += "\"firmware_version\":\"" + String(FIRMWARE_VERSION) + "\",";
  body += "\"signature\":\"" + signature + "\"";
  body += "}";

  String response;
  return postJson("/api/esp32/heartbeat", body, response);
}

void consultarComandos() {
  String ts = unixTimestamp();
  String n = nonce();
  String payload = String(DEVICE_ID) + "|" + ts + "|" + n;
  String signature = hmacSha256(payload, DEVICE_SECRET);

  String body = "{";
  body += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  body += "\"ts\":\"" + ts + "\",";
  body += "\"nonce\":\"" + n + "\",";
  body += "\"signature\":\"" + signature + "\"";
  body += "}";

  String response;
  if (!postJson("/api/esp32/commands", body, response)) {
    return;
  }

  if (response.indexOf("alert_on") >= 0) {
    aplicarAlarma(true);
  }
  if (response.indexOf("alert_off") >= 0) {
    aplicarAlarma(false);
  }
}

void conectarWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(SSID, PASSWORD);

  Serial.println();
  Serial.print("Conectando");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("Conectado");
  Serial.print("IP del ESP32: ");
  Serial.println(WiFi.localIP());
}

void esperarHoraNtp() {
  Serial.print("Sincronizando hora");
  for (int i = 0; i < 20; i++) {
    time_t now;
    time(&now);
    if (now > 1700000000) {
      Serial.println();
      Serial.println("Hora sincronizada");
      return;
    }
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("No se pudo confirmar NTP; la firma puede fallar si la hora queda en 0.");
}

void setup() {
  Serial.begin(115200);
  randomSeed(esp_random());

  pinMode(PIN_MOTOR, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  aplicarAlarma(false);

  conectarWifi();
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  esperarHoraNtp();

  server.on("/", responderEstado);
  server.on("/estado", responderEstado);
  server.on("/alerta_on", alertaOn);
  server.on("/alerta_off", alertaOff);
  server.onNotFound(rutaNoEncontrada);

  server.begin();
  Serial.println("Servidor HTTP iniciado en puerto 80");

  enviarHeartbeat();
}

void loop() {
  server.handleClient();

  if (WiFi.status() != WL_CONNECTED) {
    conectarWifi();
  }

  unsigned long now = millis();
  if (now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeat = now;
    enviarHeartbeat();
  }

  if (now - lastCommandPoll >= COMMAND_INTERVAL_MS) {
    lastCommandPoll = now;
    consultarComandos();
  }
}
