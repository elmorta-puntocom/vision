"""
Prueba del ESP32 por cable USB, sin cámara ni detección.

Uso (con el entorno virtual activo, en la carpeta del proyecto):
    python probar_esp32.py

Muestra los puertos USB, le pregunta su ID a cada placa, lo compara con
config.json y hace sonar la alarma 3 segundos. Sirve para saber si el
problema está en la conexión USB o en la detección.
"""
import json
import sys
import time
from pathlib import Path

BAUDIOS = 115200
CONFIG = Path(__file__).resolve().parent / "config.json"


def leer_lineas(puerto, segundos):
    """Devuelve todas las líneas que manda la placa durante 'segundos'."""
    lineas = []
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        crudo = puerto.readline()
        if crudo:
            lineas.append(crudo.decode("utf-8", errors="replace").strip())
    return lineas


def respuesta(lineas, inicio):
    for linea in lineas:
        pos = linea.find("VISION:")
        if pos >= 0 and linea[pos + 7:].strip().startswith(inicio):
            return linea[pos + 7:].strip()
    return None


def main():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        print("FALTA pyserial. Ejecutá: pip install pyserial")
        return 1

    id_config = None
    if CONFIG.exists():
        try:
            id_config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))["device_id"].strip().upper()
            print(f"config.json -> device_id = {id_config}")
        except Exception as exc:
            print(f"config.json no se pudo leer: {exc}")
    else:
        print("No hay config.json en la carpeta (igual se prueba el USB).")

    puertos = list(list_ports.comports())
    if not puertos:
        print("\nNo hay ningún puerto COM. Revisá el cable (tiene que ser de DATOS) y el driver CP210x/CH340.")
        return 1

    print("\nPuertos encontrados:")
    for p in puertos:
        vid = f"{p.vid:04X}" if p.vid is not None else "----"
        print(f"  {p.device:8} VID={vid}  {p.description}")

    for p in puertos:
        if p.vid is None:
            continue
        print(f"\n=== Probando {p.device} ===")
        puerto = serial.Serial()
        puerto.port = p.device
        puerto.baudrate = BAUDIOS
        puerto.timeout = 0.1
        puerto.dtr = False
        puerto.rts = False
        try:
            puerto.open()
        except Exception as exc:
            print(f"  NO SE PUDO ABRIR: {exc}")
            print("  -> Casi siempre es el Monitor Serie del Arduino IDE (o el programa de detección) abierto. Cerralo.")
            continue

        with puerto:
            puerto.reset_input_buffer()
            pong = None
            todas = []
            for _ in range(12):  # hasta 6 segundos
                puerto.write(b"PING\n")
                lineas = leer_lineas(puerto, 0.5)
                todas += lineas
                pong = respuesta(lineas, "PONG")
                if pong:
                    break

            if not pong:
                print("  La placa no respondió al PING en 6 segundos.")
                if todas:
                    print("  Lo que sí mandó:")
                    for linea in todas[-10:]:
                        print("    | " + linea)
                    print("  -> Si se ven mensajes pero no PONG, el firmware cargado es viejo: volvé a cargar esp32_alerta_wifi.ino.")
                else:
                    print("  -> No mandó nada: revisá que sea el puerto del ESP32 y que el firmware esté cargado (115200 baudios).")
                continue

            id_placa = pong[4:].strip().upper()
            print(f"  Respondió: {pong}")
            id_distinto = bool(id_config) and id_placa != id_config
            if id_distinto:
                print(f"  ID DISTINTO: la placa es {id_placa} y config.json dice {id_config}.")
                print("  -> El programa de detección ignora esta placa. Descargá el config.json del dispositivo")
                print("     correcto en Mis dispositivos, o cargá en el firmware el DEVICE_ID que corresponde.")
            else:
                print("  ID correcto.")

            print("  Encendiendo la alarma 3 segundos...")
            ok = True
            for _ in range(3):
                puerto.reset_input_buffer()
                puerto.write(b"ALERTA_ON\n")
                if not respuesta(leer_lineas(puerto, 1.0), "OK ALERTA_ON"):
                    ok = False
            puerto.write(b"ALERTA_OFF\n")
            apagado = respuesta(leer_lineas(puerto, 1.0), "OK ALERTA_OFF")

            if ok and apagado:
                print("  OK: la placa confirmó ALERTA_ON y ALERTA_OFF.")
                print("  -> Si NO sonó ni vibró, el problema es del circuito (cables, transistor, buzzer).")
                if id_distinto:
                    print("  -> Si sonó, el USB funciona, pero el programa de detección no usa esta placa")
                    print("     porque el ID no coincide con config.json (ver arriba).")
                else:
                    print("  -> Si sonó, el USB funciona: el problema está en la detección (ver la terminal del programa).")
            else:
                print("  La placa no confirmó las órdenes de alarma: volvé a cargar el firmware.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
