// Plantilla de configuración de fábrica de cada ESP32.
//
// 1. Copiá este archivo como "config_dispositivo.h" en la misma carpeta.
// 2. Completá DEVICE_ID y DEVICE_SECRET con lo que imprime:
//      python scripts/create_device.py
// 3. config_dispositivo.h está en .gitignore: el secreto NO se sube al repo.
//
// El WiFi ya no se configura acá: se carga desde el portal "VISION-Setup-...".

#pragma once

// Identificador único de la unidad (también define el nombre mDNS
// vision-<device_id>.local, en minúsculas).
#define DEVICE_ID "ESP32-XXXXXX"

// Secreto compartido con Flask para firmar el heartbeat con HMAC-SHA256.
#define DEVICE_SECRET "reemplazar-por-el-secreto-generado"

// Dirección del servidor Flask SOLO como último recurso: el ESP32 lo encuentra
// solo en la red por mDNS (servicio "_vision._tcp"), así que no hace falta
// configurar el router ni escribir la IP de la PC. No hace falta cambiar este valor.
#define SERVER_BASE_URL_DEFAULT "http://192.168.1.100:5050"

// La versión del firmware se define en esp32_alerta_wifi.ino.
