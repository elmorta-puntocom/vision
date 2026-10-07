"""
Anuncio del servidor VISION en la red local (mDNS / Zeroconf).

El ESP32 busca el servicio "_vision._tcp" y asi encuentra a Flask aunque la IP de
la PC cambie, sin que el comprador tenga que configurar el router ni escribir
ninguna direccion.
"""

import re
import socket
import threading

from . import logger


SERVICE_TYPE = '_vision._tcp.local.'
# Cada cuantos segundos se revisa si la PC cambio de IP (otro WiFi, DHCP, etc.).
REFRESCO_SEGUNDOS = 10.0


def _es_ip_util(ip):
    # 127.x es la propia PC y 169.254.x es "sin DHCP": el ESP32 no llega a ninguna.
    return bool(ip) and not ip.startswith(('127.', '169.254.', '0.'))


# Adaptadores que NO son la red local real: con una VPN activa la ruta principal
# sale por ella (p. ej. 10.2.0.2) y el ESP32 jamas podria alcanzar esa IP.
_ADAPTADORES_VIRTUALES = (
    'tun', 'tap', 'vpn', 'proton', 'wireguard', 'wintun', 'vethernet', 'hyper-v',
    'vmware', 'virtualbox', 'vbox', 'bluetooth', 'loopback', 'wsl', 'docker', 'npcap',
)


def _ip_de_salida():
    """IP de la interfaz que el sistema usaria para salir a internet, o None."""
    try:
        # connect() sobre UDP no envia nada: solo hace que el sistema elija la interfaz.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            ip = sock.getsockname()[0]
        return ip if _es_ip_util(ip) else None
    except OSError:
        return None  # sin ruta por defecto (LAN sin internet)


def _ips_de_redes_locales():
    """IPs de los adaptadores reales (sin VPN ni virtuales), o None si no se pueden listar."""
    try:
        import ifaddr  # viene con zeroconf
    except ImportError:
        return None

    ips = []
    for adaptador in ifaddr.get_adapters():
        nombre = f'{adaptador.nice_name} {adaptador.name}'.lower()
        if any(palabra in nombre for palabra in _ADAPTADORES_VIRTUALES):
            continue
        for direccion in adaptador.ips:
            if direccion.is_IPv4 and _es_ip_util(direccion.ip):
                ips.append(direccion.ip)
    return ips


def ip_principal():
    """IP de la PC en su red local real (no la de una VPN), o None si no hay red."""
    salida = _ip_de_salida()
    reales = _ips_de_redes_locales()
    if reales:
        return salida if salida in reales else reales[0]
    if salida:
        return salida

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if _es_ip_util(info[4][0]):
                return info[4][0]
    except OSError:
        pass
    return None


class AnuncioServidor:
    """Mantiene publicado "_vision._tcp" apuntando a la IP actual de la PC."""

    def __init__(self, puerto):
        self.puerto = puerto
        self._detener = threading.Event()
        self._hilo = None
        self._zc = None
        self._info = None
        self.ip_anunciada = None

    def iniciar(self):
        try:
            import zeroconf  # noqa: F401
        except ImportError:
            logger.warning(
                '[DISCOVERY] Falta la libreria zeroconf (pip install zeroconf): '
                'el ESP32 no podra encontrar el servidor solo si la IP de la PC cambia.'
            )
            return False

        self._hilo = threading.Thread(target=self._run, daemon=True, name='AnuncioMdns')
        self._hilo.start()
        return True

    def detener(self):
        self._detener.set()
        if self._hilo is not None:
            self._hilo.join(timeout=5.0)
        self._cerrar()

    def _run(self):
        while True:
            ip = ip_principal()
            if ip != self.ip_anunciada:
                self._cerrar()
                if ip:
                    self._anunciar(ip)
                else:
                    logger.warning('[DISCOVERY] La PC no tiene red; se anunciara al conectarse.')
                    self.ip_anunciada = None
            if self._detener.wait(REFRESCO_SEGUNDOS):
                break

    def _anunciar(self, ip):
        from zeroconf import ServiceInfo, Zeroconf

        host = re.sub(r'[^a-z0-9]+', '-', socket.gethostname().lower()).strip('-') or 'pc'
        info = ServiceInfo(
            SERVICE_TYPE,
            f'VISION Server {host}.{SERVICE_TYPE}',
            addresses=[socket.inet_aton(ip)],
            port=self.puerto,
            properties={'app': 'vision'},
            server=f'vision-server-{host}.local.',
        )
        try:
            # Zeroconf() abre sockets en todas las interfaces de red de ese momento,
            # por eso se recrea completo cada vez que cambia la IP.
            zc = Zeroconf()
            zc.register_service(info, allow_name_change=True)
        except Exception as exc:
            logger.warning(f'[DISCOVERY] No se pudo anunciar el servidor en la red: {exc}')
            self.ip_anunciada = None
            return

        self._zc, self._info, self.ip_anunciada = zc, info, ip
        logger.info(
            f'[DISCOVERY] Servidor anunciado en la red como {SERVICE_TYPE} -> {ip}:{self.puerto}'
        )

    def _cerrar(self):
        zc, info = self._zc, self._info
        self._zc = self._info = None
        if zc is None:
            return
        try:
            zc.unregister_service(info)
        except Exception:
            pass
        try:
            zc.close()
        except Exception:
            pass


def iniciar_anuncio_servidor(puerto):
    """Publica el servidor en la red local. Devuelve el anuncio (o None sin zeroconf)."""
    anuncio = AnuncioServidor(puerto)
    return anuncio if anuncio.iniciar() else None
