#include <WiFi.h>
#include <WebServer.h>

const char* SSID = "placa";
const char* PASSWORD = "123454678";

// Pines reales solicitados:
// P23 controla el motor vibrador.
// P22 controla el buzzer activo.
const int PIN_MOTOR = 23;
const int PIN_BUZZER = 22;

WebServer server(80);

bool alarmaActiva = false;
unsigned long ultimoIntentoWifi = 0;

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

void conectarWifiSinBloquear() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  unsigned long ahora = millis();
  if (ahora - ultimoIntentoWifi < 1000) {
    return;
  }

  ultimoIntentoWifi = ahora;
  Serial.println("Conectando a WiFi...");
  WiFi.begin(SSID, PASSWORD);
}

void setup() {
  Serial.begin(115200);

  pinMode(PIN_MOTOR, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  aplicarAlarma(false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(SSID, PASSWORD);

  Serial.println();
  Serial.println("Esperando conexion WiFi...");
  while (WiFi.status() != WL_CONNECTED) {
    conectarWifiSinBloquear();
    yield();
  }

  Serial.println("WiFi conectado");
  Serial.print("IP del ESP32: ");
  Serial.println(WiFi.localIP());

  server.on("/", responderEstado);
  server.on("/estado", responderEstado);
  server.on("/alerta_on", alertaOn);
  server.on("/alerta_off", alertaOff);
  server.onNotFound(rutaNoEncontrada);

  server.begin();
  Serial.println("Servidor HTTP iniciado en puerto 80");
}

void loop() {
  conectarWifiSinBloquear();
  server.handleClient();
}
