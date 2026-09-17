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

// Arquitectura:
// - La ALARMA se controla por el cable USB (serial), atendido en loop().
//   No depende de ninguna red, así que funciona igual dentro del auto.
// - El WiFi (heartbeat, comandos web, rutas HTTP, mDNS) corre en una tarea
//   aparte del otro núcleo: si la red no está o el servidor tarda, la alarma
//   por USB no se demora.

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

// Tiempo que queda abierto el portal cautivo cuando no hay WiFi configurado.
// Al vencer, el dispositivo sigue funcionando por USB sin WiFi.
const int PORTAL_TIMEOUT_SEGUNDOS = 180;

const unsigned long HEARTBEAT_INTERVAL_MS = 30000;
const unsigned long COMMAND_INTERVAL_MS = 5000;

// Sin WiFi se vuelve a buscar la red guardada cada tanto. El reconectado
// automático del core no reintenta si la red "no se encuentra" (fuera de rango),
// así que sin esto el ESP32 no volvería a conectarse al llegar a la casa.
const unsigned long WIFI_REINTENTO_MS = 30000;

// Si el servidor no responde se espera cada vez más antes de reintentar.
const unsigned long SERVIDOR_REINTENTO_MIN_MS = 5000;
const unsigned long SERVIDOR_REINTENTO_MAX_MS = 60000;

// Seguridad: la alarma se apaga sola si nadie la vuelve a pedir en este tiempo
// (el script la refresca cada 1 s mientras dura la somnolencia). Evita que el
// buzzer quede sonando si se cae la PC, el script o la conexión.
const unsigned long ALARMA_SIN_REFRESCO_MS = 5000;

// Protocolo USB: el script manda una orden por línea y el ESP32 responde con
// líneas que empiezan con "VISION:" (el resto del Serial son logs).
const char* PREFIJO_SERIAL = "VISION:";
const size_t MAX_LINEA_SERIAL = 64;

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
String hostnameDispositivo;
volatile bool alarmaActiva = false;
volatile unsigned long ultimaOrdenAlarma = 0;
bool wifiEstabaConectado = false;
bool servidorWebIniciado = false;
bool heartbeatPendiente = false;
unsigned long lastHeartbeat = 0;
unsigned long lastCommandPoll = 0;
unsigned long proximoIntentoServidor = 0;
unsigned long esperaServidorMs = SERVIDOR_REINTENTO_MIN_MS;
unsigned long ultimoIntentoWifi = 0;
String lineaSerial = "";

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

bool horaSincronizada() {
  time_t now;
  time(&now);
  return now > 1700000000;
}

void aplicarAlarma(bool activar) {
  alarmaActiva = activar;

  digitalWrite(PIN_MOTOR, activar ? HIGH : LOW);
  digitalWrite(PIN_BUZZER, activar ? HIGH : LOW);

  Serial.println(activar ? "Alarma ACTIVADA" : "Alarma DESACTIVADA");
}

// Punto único para las órdenes externas (USB, rutas HTTP o comandos web):
// renueva el tiempo de seguridad y solo cambia los pines si cambia el estado.
void ordenarAlarma(bool activar) {
  ultimaOrdenAlarma = millis();
  if (alarmaActiva != activar) {
    aplicarAlarma(activar);
  }
}

void revisarSeguridadAlarma() {
  if (alarmaActiva && millis() - ultimaOrdenAlarma > ALARMA_SIN_REFRESCO_MS) {
    Serial.println("[SEGURIDAD] Sin órdenes recientes: se apaga la alarma.");
    aplicarAlarma(false);
  }
}

void responderEstado() {
  String estado = alarmaActiva ? "ON" : "OFF";
  server.send(200, "text/plain", "ESP32 OK - Alarma: " + estado);
}

void alertaOn() {
  ordenarAlarma(true);
  server.send(200, "text/plain", "Alerta ACTIVADA");
}

void alertaOff() {
  ordenarAlarma(false);
  server.send(200, "text/plain", "Alerta DESACTIVADA");
}

void rutaNoEncontrada() {
  server.send(404, "text/plain", "Ruta no encontrada");
}

// ── Protocolo USB (serial) ──────────────────────────────────────────────────

void responderSerial(String mensaje) {
  // Una sola escritura por línea para que no se mezcle con logs de la otra tarea.
  String linea = String(PREFIJO_SERIAL) + mensaje + "\n";
  Serial.print(linea);
}

void procesarOrdenSerial(String orden) {
  orden.trim();
  orden.toUpperCase();
  if (orden.length() == 0) {
    return;
  }

  if (orden == "PING") {
    responderSerial("PONG " + String(DEVICE_ID));
  } else if (orden == "ALERTA_ON") {
    ordenarAlarma(true);
    responderSerial("OK ALERTA_ON");
  } else if (orden == "ALERTA_OFF") {
    ordenarAlarma(false);
    responderSerial("OK ALERTA_OFF");
  } else if (orden == "ESTADO") {
    String wifi = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "SIN_WIFI";
    responderSerial(String("ESTADO ") + (alarmaActiva ? "ON " : "OFF ") + wifi);
  } else {
    responderSerial("ERROR " + orden);
  }
}

void leerSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineaSerial.length() > 0) {
        procesarOrdenSerial(lineaSerial);
        lineaSerial = "";
      }
    } else if (lineaSerial.length() < MAX_LINEA_SERIAL) {
      lineaSerial += c;
    } else {
      lineaSerial = "";  // Línea demasiado larga: basura en el puerto.
    }
  }
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
  MDNS.end();
  if (!MDNS.begin(hostnameDispositivo.c_str())) {
    Serial.println("[mDNS] No se pudo iniciar el respondedor mDNS");
    return;
  }
  MDNS.addService("http", "tcp", 80);
  Serial.printf("[mDNS] Anunciado como %s.local\n", hostnameDispositivo.c_str());
}

// ── Configuración WiFi (WiFiManager, sin bloquear) ──────────────────────────

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

void iniciarWifi(bool forzarPortal) {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.setHostname(hostnameDispositivo.c_str());

  strncpy(urlServidorPortal, serverBaseUrl.c_str(), sizeof(urlServidorPortal) - 1);
  urlServidorPortal[sizeof(urlServidorPortal) - 1] = '\0';
  parametroServidor.setValue(urlServidorPortal, sizeof(urlServidorPortal) - 1);

  wifiManager.addParameter(&parametroServidor);
  wifiManager.setSaveConfigCallback(alGuardarPortal);
  wifiManager.setConfigPortalTimeout(PORTAL_TIMEOUT_SEGUNDOS);
  wifiManager.setConfigPortalBlocking(false);
  wifiManager.setHostname(hostnameDispositivo.c_str());

  // El portal solo se abre si no hay WiFi guardado o se pidió reset. Si hay
  // credenciales pero la red no está (p. ej. en el auto), NO se abre el portal:
  // el ESP32 sigue funcionando por USB y reconecta solo al volver a la casa.
  if (forzarPortal || !wifiManager.getWiFiIsSaved()) {
    String nombrePortal = "VISION-Setup-" + String(DEVICE_ID);
    Serial.printf("[WiFi] Sin WiFi configurado: portal \"%s\" abierto por %d s\n",
                  nombrePortal.c_str(), PORTAL_TIMEOUT_SEGUNDOS);
    wifiManager.startConfigPortal(nombrePortal.c_str());
  } else {
    Serial.println("[WiFi] Conectando en segundo plano a la red guardada");
    WiFi.begin();
    ultimoIntentoWifi = millis();
  }
}

void guardarUrlDelPortal() {
  if (!guardarParametrosPortal) {
    return;
  }
  guardarParametrosPortal = false;

  String nuevaUrl = String(parametroServidor.getValue());
  nuevaUrl.trim();
  while (nuevaUrl.endsWith("/")) {
    nuevaUrl.remove(nuevaUrl.length() - 1);
  }
  if (nuevaUrl.length() > 0) {
    serverBaseUrl = nuevaUrl;
    preferencias.putString("server_url", serverBaseUrl);
    Serial.println("[WiFi] URL del servidor guardada: " + serverBaseUrl);
  }
}

// ── Comunicación con Flask ──────────────────────────────────────────────────

// Devuelve el código HTTP, o un valor negativo si no hubo conexión.
int postJson(String endpoint, String body, String &response) {
  HTTPClient http;
  String url = serverBaseUrl + endpoint;

  if (!http.begin(url)) {
    Serial.printf("[API] URL inválida: %s\n", url.c_str());
    return -1;
  }
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

  return code;
}

// Registra el resultado para espaciar los reintentos si el servidor no responde.
void registrarRespuestaServidor(int code) {
  if (code > 0) {
    esperaServidorMs = SERVIDOR_REINTENTO_MIN_MS;
    proximoIntentoServidor = 0;
    return;
  }
  proximoIntentoServidor = millis() + esperaServidorMs;
  Serial.printf("[API] Servidor inaccesible; próximo intento en %lu s\n", esperaServidorMs / 1000);
  esperaServidorMs = min(esperaServidorMs * 2, SERVIDOR_REINTENTO_MAX_MS);
}

int enviarHeartbeat() {
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

int consultarComandos() {
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
  int code = postJson("/api/esp32/commands", body, response);
  if (code < 200 || code >= 300) {
    return code;
  }

  if (response.indexOf("alert_on") >= 0) {
    ordenarAlarma(true);
  }
  if (response.indexOf("alert_off") >= 0) {
    ordenarAlarma(false);
  }
  return code;
}

// ── Tarea de red (núcleo 0) ─────────────────────────────────────────────────

void tareaRed(void* parametro) {
  for (;;) {
    if (wifiManager.getConfigPortalActive()) {
      wifiManager.process();
      guardarUrlDelPortal();
    }

    // Al (re)conectar se re-anuncia por mDNS y se avisa la IP, que puede cambiar.
    bool wifiConectado = WiFi.status() == WL_CONNECTED;
    if (wifiConectado && !wifiEstabaConectado) {
      Serial.print("[WiFi] Conectado. IP del ESP32: ");
      Serial.println(WiFi.localIP());
      iniciarMdns();
      heartbeatPendiente = true;
      proximoIntentoServidor = 0;
      esperaServidorMs = SERVIDOR_REINTENTO_MIN_MS;
    } else if (!wifiConectado && wifiEstabaConectado) {
      Serial.println("[WiFi] Conexión perdida; la alarma sigue funcionando por USB.");
    }
    wifiEstabaConectado = wifiConectado;

    // Reintento periódico: no bloquea (la conexión sigue en segundo plano).
    if (!wifiConectado
        && !wifiManager.getConfigPortalActive()
        && wifiManager.getWiFiIsSaved()
        && millis() - ultimoIntentoWifi >= WIFI_REINTENTO_MS) {
      ultimoIntentoWifi = millis();
      Serial.println("[WiFi] Buscando la red guardada...");
      WiFi.disconnect();
      WiFi.begin();
    }

    if (wifiConectado) {
      // El portal de WiFiManager usa el mismo puerto 80: el servidor propio
      // arranca recién cuando el portal está cerrado.
      if (!servidorWebIniciado && !wifiManager.getConfigPortalActive()) {
        server.begin();
        servidorWebIniciado = true;
        Serial.println("Servidor HTTP iniciado en puerto 80");
      }
      if (servidorWebIniciado) {
        server.handleClient();
      }

      unsigned long now = millis();
      // La firma HMAC necesita la hora real (NTP); sin ella el servidor la rechaza.
      bool servidorDisponible = horaSincronizada()
        && (proximoIntentoServidor == 0 || now >= proximoIntentoServidor);

      if (servidorDisponible && (heartbeatPendiente || now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS)) {
        heartbeatPendiente = false;
        lastHeartbeat = now;
        registrarRespuestaServidor(enviarHeartbeat());
      } else if (servidorDisponible && now - lastCommandPoll >= COMMAND_INTERVAL_MS) {
        lastCommandPoll = now;
        registrarRespuestaServidor(consultarComandos());
      }
    }

    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

void setup() {
  Serial.begin(115200);
  randomSeed(esp_random());

  pinMode(PIN_MOTOR, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  aplicarAlarma(false);

  hostnameDispositivo = nombreMdns();
  preferencias.begin("vision", false);
  serverBaseUrl = preferencias.getString("server_url", SERVER_BASE_URL_DEFAULT);

  bool resetPedido = botonResetMantenido();
  if (resetPedido) {
    borrarConfiguracionRed();
  }

  iniciarWifi(resetPedido);

  // La hora se sincroniza sola cuando haya internet; no se espera acá.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  server.on("/", responderEstado);
  server.on("/estado", responderEstado);
  server.on("/alerta_on", alertaOn);
  server.on("/alerta_off", alertaOff);
  server.onNotFound(rutaNoEncontrada);

  xTaskCreatePinnedToCore(tareaRed, "VisionRed", 12288, NULL, 1, NULL, 0);

  Serial.println("Servidor VISION: " + serverBaseUrl);
  responderSerial("LISTO " + String(DEVICE_ID));
}

void loop() {
  // Núcleo 1: solo USB y seguridad de la alarma, nunca se bloquea por la red.
  leerSerial();
  revisarSeguridadAlarma();
  delay(5);
}
