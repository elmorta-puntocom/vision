/*
 * Estado Online/Offline en vivo de los ESP32.
 *
 * Cada 3 segundos consulta /api/panel/estado y actualiza, sin recargar la
 * página, los elementos marcados con atributos data-* (ver dashboard.html y
 * dispositivos.html):
 *   data-estado-dispositivo="<ID>"   -> "Online" / "Offline"
 *   data-detalle-dispositivo="<ID>"  -> canal: "vía USB", "vía WiFi", "vía USB + WiFi"
 *   data-ultima-conexion="<ID>"      -> última señal recibida (hora argentina)
 *   data-ip-dispositivo="<ID>"       -> IP informada por el ESP32
 * Un ESP32 aparece Online si dio señales en los últimos 20 segundos por WiFi
 * (heartbeat / consulta de comandos) o por USB (informado por la notebook).
 */
(function () {
    'use strict';

    const raiz = document.getElementById('panel-vivo');
    if (!raiz) {
        return;
    }

    const URL_ESTADO = raiz.dataset.url;
    const INTERVALO_MS = 3000;
    let fallosSeguidos = 0;

    function todos(selector) {
        return Array.from(document.querySelectorAll(selector));
    }

    function actualizarDispositivos(dispositivos) {
        dispositivos.forEach(function (d) {
            todos('[data-estado-dispositivo="' + d.device_id + '"]').forEach(function (el) {
                el.textContent = d.texto;
                el.classList.toggle('offline', !d.online);
            });
            todos('[data-detalle-dispositivo="' + d.device_id + '"]').forEach(function (el) {
                el.textContent = d.detalle;
            });
            todos('[data-ultima-conexion="' + d.device_id + '"]').forEach(function (el) {
                el.textContent = d.ultima_conexion || 'Nunca';
            });
            todos('[data-ip-dispositivo="' + d.device_id + '"]').forEach(function (el) {
                el.textContent = d.ip_address || 'Sin registrar';
            });
        });

        const resumen = document.getElementById('resumen-dispositivos');
        if (resumen && dispositivos.length) {
            const online = dispositivos.filter(function (d) { return d.online; });
            if (online.length) {
                resumen.textContent = 'Online · ' + online.map(function (d) {
                    return d.device_id + ' (' + d.canales.join(' + ') + ')';
                }).join(', ');
                resumen.classList.remove('offline');
            } else {
                resumen.textContent = 'Offline · ningún ESP32 conectado';
                resumen.classList.add('offline');
            }
        }
    }

    function estadoServidor(conectado, hora) {
        const el = document.getElementById('estado-servidor');
        if (!el) {
            return;
        }
        el.classList.toggle('offline', !conectado);
        el.querySelector('[data-texto]').textContent = conectado
            ? 'Servidor y base de datos conectados · ' + hora
            : 'Sin respuesta del servidor (reintentando)';
    }

    async function consultar() {
        try {
            const respuesta = await fetch(URL_ESTADO, {
                headers: { 'Accept': 'application/json' },
                cache: 'no-store'
            });
            if (respuesta.redirected || respuesta.status === 401) {
                return; // Sesión cerrada: se deja de consultar.
            }
            if (!respuesta.ok) {
                throw new Error('HTTP ' + respuesta.status);
            }
            const datos = await respuesta.json();
            fallosSeguidos = 0;
            estadoServidor(true, datos.hora_servidor);
            actualizarDispositivos(datos.dispositivos || []);
        } catch (error) {
            fallosSeguidos += 1;
            if (fallosSeguidos >= 2) {
                estadoServidor(false);
            }
        }
        setTimeout(consultar, INTERVALO_MS);
    }

    consultar();
})();
