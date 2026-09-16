#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WiFiManager.h>  // Librería "WiFiManager" de tzapu
#include "mbedtls/md.h"
#include <time.h>

// DEVICE_ID, DEVICE_SECRET, SERVER_BASE_URL_DEFAULT y FIRMWARE_VERSION.
// Copiá config_dispositivo.example.h como config_dispositivo.h y completalo.
#include "config_dispositivo.h"

// Pines reales solicitados:
// P23 controla el motor vibrador.
// P22 controla el buzzer activo.
const int PIN_MOTOR = 23;
const int PIN_BUZZER = 22;

// Pulsador a GND para borrar el WiFi guardado (usa la pull-up interna).
// Se evita GPIO0 (BOOT) porque mantenerlo al encender pone al ESP32 en modo
// de programación. Mantenerlo presionado al bootear durante TIEMPO_RESET_MS.
const int PIN_RESET_WIFI = 4;
const unsigned long TIEMPO_RESET_MS = 3000;

// Tiempo máximo del portal cautivo antes de reiniciar y volver a intentar.
const int PORTAL_TIMEOUT_SEGUNDOS = 180;

const unsigned long HEARTBEAT_INTERVAL_MS = 30000;
const unsigned long COMMAND_INTERVAL_MS = 5000;

WebServer server(80);
WiFiManager wifiManager;
Preferences preferencias;

// Campo extra del portal para cargar la URL del servidor Flask.
char urlServidorPortal[120] = SERVER_BASE_URL_DEFAULT;
WiFiManagerParameter parametroServidor(
  "server_url", "URL del servidor VISION", urlServidorPortal, sizeof(urlServidorPortal) - 1
);
bool guardarParametrosPortal = false;

String serverBaseUrl = SERVER_BASE_URL_DEFAULT;
bool alarmaActiva = false;
bool wifiEstabaConectado = false;
bool heartbeatPendiente = false;
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

// ── Identidad en la red local ───────────────────────────────────────────────

// Nombre mDNS: "vision-" + device_id en minúsculas, solo letras, números y guiones.
// Debe coincidir con nombre_mdns() de deteccion_tiempo_real.py.
String nombreMdns() {
  String nombre = "vision-";
  String id = String(DEVICE_ID);
  id.toLowerCase();
  for (unsigned int i = 0; i < id.length(); i++) {
    char c = id[i];
    nombre += (isalnum(c) ? c : '-');
  }
  return nombre;
}

void iniciarMdns() {
  String host = nombreMdns();
  MDNS.end();
  if (!MDNS.begin(host.c_str())) {
    Serial.println("[mDNS] No se pudo iniciar el respondedor mDNS");
    return;
  }
  MDNS.addService("http", "tcp", 80);
  Serial.printf("[mDNS] Anunciado como %s.local\n", host.c_str());
}

// ── Configuración WiFi (WiFiManager) ────────────────────────────────────────

void alGuardarPortal() {
  guardarParametrosPortal = true;
}

bool botonResetMantenido() {
  pinMode(PIN_RESET_WIFI, INPUT_PULLUP);
  delay(50);
  if (digitalRead(PIN_RESET_WIFI) != LOW) {
    return false;
  }

  Serial.println("[RESET] Botón presionado: mantenelo para borrar el WiFi guardado...");
  unsigned long inicio = millis();
  while (millis() - inicio < TIEMPO_RESET_MS) {
    if (digitalRead(PIN_RESET_WIFI) != LOW) {
      Serial.println("[RESET] Cancelado: el botón se soltó antes de tiempo.");
      return false;
    }
    delay(50);
  }
  return true;
}

void borrarConfiguracionRed() {
  Serial.println("[RESET] Borrando credenciales WiFi y URL del servidor guardadas.");
  wifiManager.resetSettings();
  preferencias.remove("server_url");
  serverBaseUrl = SERVER_BASE_URL_DEFAULT;

  // Pitido corto como confirmación física del reset.
  aplicarAlarma(true);
  delay(300);
  aplicarAlarma(false);
}

void conectarWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  strncpy(urlServidorPortal, serverBaseUrl.c_str(), sizeof(urlServidorPortal) - 1);
  urlServidorPortal[sizeof(urlServidorPortal) - 1] = '\0';
  parametroServidor.setValue(urlServidorPortal, sizeof(urlServidorPortal) - 1);

  wifiManager.addParameter(&parametroServidor);
  wifiManager.setSaveConfigCallback(alGuardarPortal);
  wifiManager.setConfigPortalTimeout(PORTAL_TIMEOUT_SEGUNDOS);
  wifiManager.setHostname(nombreMdns().c_str());

  // Si hay credenciales en flash conecta directo; si no, levanta el portal.
  String nombrePortal = "VISION-Setup-" + String(DEVICE_ID);
  Serial.println();
  Serial.printf("Conectando WiFi (portal si hace falta: %s)\n", nombrePortal.c_str());

  if (!wifiManager.autoConnect(nombrePortal.c_str())) {
    Serial.println("No se pudo conectar ni configurar el WiFi. Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  if (guardarParametrosPortal) {
    String nuevaUrl = String(parametroServidor.getValue());
    nuevaUrl.trim();
    while (nuevaUrl.endsWith("/")) {
      nuevaUrl.remove(nuevaUrl.length() - 1);
    }
    if (nuevaUrl.length() > 0) {
      serverBaseUrl = nuevaUrl;
      preferencias.putString("server_url", serverBaseUrl);
      Serial.println("URL del servidor guardada: " + serverBaseUrl);
    }
    guardarParametrosPortal = false;
  }

  Serial.println("Conectado");
  Serial.print("IP del ESP32: ");
  Serial.println(WiFi.localIP());
}

// ── Comunicación con Flask ──────────────────────────────────────────────────

bool postJson(String endpoint, String body, String &response) {
  HTTPClient http;
  String url = serverBaseUrl + endpoint;

  if (!http.begin(url)) {
    Serial.printf("[API] URL inválida: %s\n", url.c_str());
    return false;
  }
  // Timeouts cortos para no bloquear las rutas /alerta_on y /alerta_off.
  http.setConnectTimeout(2000);
  http.setTimeout(3000);
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
  // IP dentro de la LAN: el servidor puede estar detrás de NAT y ver otra IP.
  body += "\"local_ip\":\"" + WiFi.localIP().toString() + "\",";
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

  preferencias.begin("vision", false);
  serverBaseUrl = preferencias.getString("server_url", SERVER_BASE_URL_DEFAULT);

  if (botonResetMantenido()) {
    borrarConfiguracionRed();
  }

  conectarWifi();
  wifiEstabaConectado = true;
  iniciarMdns();

  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  esperarHoraNtp();

  server.on("/", responderEstado);
  server.on("/estado", responderEstado);
  server.on("/alerta_on", alertaOn);
  server.on("/alerta_off", alertaOff);
  server.onNotFound(rutaNoEncontrada);

  server.begin();
  Serial.println("Servidor HTTP iniciado en puerto 80");
  Serial.println("Servidor VISION: " + serverBaseUrl);

  enviarHeartbeat();
  lastHeartbeat = millis();
}

void loop() {
  server.handleClient();

  // El ESP32 reconecta solo con las credenciales guardadas; al volver la
  // conexión se re-anuncia por mDNS y se avisa la IP (puede haber cambiado).
  bool wifiConectado = WiFi.status() == WL_CONNECTED;
  if (wifiConectado && !wifiEstabaConectado) {
    Serial.print("WiFi reconectado. IP del ESP32: ");
    Serial.println(WiFi.localIP());
    iniciarMdns();
    heartbeatPendiente = true;
  }
  wifiEstabaConectado = wifiConectado;

  if (!wifiConectado) {
    return;
  }

  unsigned long now = millis();
  if (heartbeatPendiente || now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
    heartbeatPendiente = false;
    lastHeartbeat = now;
    enviarHeartbeat();
  }

  if (now - lastCommandPoll >= COMMAND_INTERVAL_MS) {
    lastCommandPoll = now;
    consultarComandos();
  }
}
