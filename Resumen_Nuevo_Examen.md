# Resumen actualizado para el nuevo examen - Proyecto VISION

Este resumen explica las funciones nuevas que tiene el codigo actual del proyecto. Esta pensado para responder en el oral de forma clara, ubicando cada parte dentro del patron MVC y relacionandola con la base de datos, la deteccion en tiempo real y el ESP32.

---

## 1. Que es VISION actualmente

VISION es un sistema de seguridad vial que combina:

* vision artificial para detectar somnolencia;
* una aplicacion web Flask;
* base de datos MySQL;
* respaldo offline con SQLite;
* un dispositivo ESP32 para alarma fisica;
* dashboard, historial, roles, panel admin y gestion de usuarios.

Respuesta corta para el oral:

> "VISION detecta ojos cerrados y cabeceo con una camara, MediaPipe, EAR, pitch y TensorFlow. Cuando detecta riesgo, activa un ESP32, graba evidencia tipo caja negra y reporta el evento al backend Flask, donde queda guardado en la base de datos y se muestra en el dashboard."

---

## 2. Arquitectura MVC

El proyecto usa Flask siguiendo una separacion tipo MVC:

* **Modelo:** `app/models.py`
* **Controlador:** `app/controllers.py`
* **Vistas:** `app/templates/`
* **Servicios auxiliares:** `app/services.py`
* **Archivos estaticos:** `app/static/`

Ejemplo:

* La tabla de usuarios esta representada por la clase `Usuario` en `models.py`.
* La ruta `/dashboard` esta en `controllers.py`.
* La pantalla visual esta en `templates/dashboard.html`.

Respuesta corta:

> "El modelo define las tablas con SQLAlchemy, el controlador recibe requests y consulta la base, y las vistas muestran los datos con Jinja."

---

## 3. Sistema de usuarios, login y seguridad

El sistema maneja usuarios con:

* registro;
* login;
* logout;
* recuperacion de contrasena;
* contrasenas hasheadas con Bcrypt;
* sesiones con Flask-Login.

Archivos:

* `app/models.py`
* `app/controllers.py`
* `app/templates/login.html`
* `app/templates/register.html`
* `app/templates/forgot_password.html`
* `app/templates/reset_password.html`

La contrasena nunca se guarda en texto plano. En el modelo `Usuario` se usan:

```python
def set_password(self, pw):
    self.password_hash = bcrypt.generate_password_hash(pw).decode('utf-8')

def check_password(self, pw):
    return bcrypt.check_password_hash(self.password_hash, pw)
```

Respuesta corta:

> "La seguridad de usuarios se basa en Flask-Login para sesiones y Bcrypt para contrasenas. La base guarda el hash, no la contrasena real."

---

## 4. Recuperacion de contrasena

Rutas:

* `/forgot-password`
* `/reset-password/<token>`

Templates:

* `forgot_password.html`
* `reset_password.html`

El sistema usa un token firmado y temporal. Si el token esta vencido o fue alterado, no permite cambiar la clave.

Respuesta corta:

> "La recuperacion no envia la contrasena por mail. Envia un link con token temporal firmado. Si el token es valido, se permite crear una nueva contrasena, que se guarda hasheada."

---

## 5. Roles: Administrador y Usuario comun

El sistema ahora tiene roles.

Tablas:

* `usuarios`
* `roles`
* `usuario_roles`

Archivos:

* `app/models.py`
* `vision_db_roles.sql`

Relacion:

* un usuario puede tener varios roles;
* un rol puede estar en muchos usuarios;
* por eso se usa una tabla intermedia `usuario_roles`.

Metodo importante:

```python
def has_role(self, nombre):
    return any(rol.nombre == nombre for rol in self.roles)
```

Respuesta corta:

> "Los roles separan permisos. Un usuario comun puede usar su cuenta y ver sus datos. Un administrador puede entrar al panel admin y modificar roles."

---

## 6. Modificacion de usuarios

Vista:

* `app/templates/editar_usuario.html`

Ruta:

* `/usuarios/<int:user_id>/editar`

Funcionalidades:

* editar nombre y apellido;
* cambiar contrasena si se completa el campo;
* validar email;
* validar contrasena;
* editar roles si el usuario logueado es administrador;
* bloquear acceso indebido con `abort(403)`.

Regla:

* usuario comun: solo puede editar su propio perfil;
* administrador: puede editar otros usuarios y sus roles.

Respuesta corta:

> "La edicion de usuario esta integrada al MVC. El formulario esta en la vista, el controlador valida y guarda, y el modelo representa la tabla. Ademas, el backend controla permisos."

---

## 7. Panel de administracion

Vista:

* `app/templates/admin_base_datos.html`

Ruta:

* `/admin/base-datos`

Solo entra un usuario con rol `Administrador`.

Muestra:

* usuario con sesion activa;
* total de usuarios;
* total de detecciones;
* score promedio;
* lista de usuarios;
* roles de cada usuario;
* detecciones por usuario;
* ultimas detecciones;
* boton para editar usuarios.

Respuesta corta:

> "El panel admin permite ver informacion de la base desde la web, pero esta protegido por rol. No depende solo de ocultar botones: el controlador verifica que el usuario sea administrador."

---

## 8. Dashboard actualizado

Vista:

* `app/templates/dashboard.html`

El dashboard muestra:

* nombre y email del conductor;
* score de conduccion;
* detecciones totales;
* estado del dispositivo ESP32;
* perfil del usuario;
* ultimas 20 detecciones;
* acceso a videos de evidencia;
* boton para modificar usuario;
* boton para historial;
* boton para mis dispositivos;
* boton para panel admin si corresponde.

Tambien muestra si hay detecciones pendientes de sincronizar desde SQLite.

Respuesta corta:

> "El dashboard funciona como centro de control. Muestra datos del usuario, telemetria, estado del ESP32, score y ultimas detecciones."

---

## 9. Historial de detecciones

Ruta:

* `/historial`

Vista:

* `app/templates/historial.html`

Sirve para ver eventos anteriores con mas detalle que en el dashboard.

Cada deteccion puede tener:

* fecha y hora;
* tipo de evento;
* EAR;
* pitch;
* duracion de alerta;
* dispositivo asociado;
* video de evidencia.

Respuesta corta:

> "El historial es la parte analitica. El dashboard da el resumen, y el historial permite revisar eventos con mas detalle."

---

## 10. Base de datos actual

Modelos principales en `app/models.py`:

* `Usuario`
* `Rol`
* `Deteccion`
* `EstadisticaSeguridad`
* `Dispositivo`
* `DispositivoEvento`
* `DispositivoComando`

Relaciones importantes:

* `Usuario` tiene muchas `Deteccion`.
* `Usuario` tiene una `EstadisticaSeguridad`.
* `Usuario` puede tener muchos `Dispositivo`.
* `Usuario` y `Rol` tienen relacion muchos a muchos.
* `Dispositivo` tiene eventos y comandos.
* `Deteccion` puede asociarse a un `Dispositivo`.

Respuesta corta:

> "La base no guarda solo usuarios y detecciones. Tambien administra roles, dispositivos, comandos, eventos del ESP32 y estadisticas."

---

## 11. Estadisticas y score de conduccion

Tabla:

* `estadisticas_seguridad`

Servicio:

* `registrar_deteccion_mysql` en `app/services.py`

Cada deteccion:

* aumenta `total_eventos`;
* baja el `score_conduccion`;
* actualiza `ultima_actualizacion`.

Codigo clave:

```python
stats.total_eventos = (stats.total_eventos or 0) + 1
stats.score_conduccion = max(0.0, (stats.score_conduccion or 100.0) - 2.0)
```

Respuesta corta:

> "El score empieza en 100 y baja con cada evento de riesgo. Sirve como indicador rapido del estado de conduccion."

---

## 12. Modo offline y sincronizacion

Archivo:

* `app/services.py`

Base principal:

* MySQL

Base offline:

* SQLite

Si MySQL no responde:

* la deteccion se guarda en SQLite;
* queda como pendiente;
* un hilo sincronizador intenta subirla despues;
* cuando MySQL vuelve, se migra la deteccion y se actualizan estadisticas.

Funciones:

* `guardar_deteccion_offline`
* `sincronizar_pendientes`
* `start_sync_thread`
* `mysql_disponible`

Respuesta corta:

> "El sistema no pierde eventos si se cae MySQL. Guarda temporalmente en SQLite y sincroniza despues."

---

## 13. Gestion de dispositivos ESP32

Vista:

* `app/templates/dispositivos.html`

Modelo:

* `Dispositivo`

Script:

* `scripts/create_device.py`

SQL:

* `device_linking.sql`

La tabla `dispositivos` guarda:

* `device_id`
* `mac`
* `device_secret`
* `activation_code_hash`
* `usuario_id`
* `ip_address`
* `firmware_version`
* `last_seen`
* `last_seen_usb`
* `api_key_hash`

Flujo:

1. Se registra un ESP32.
2. Se genera codigo de activacion.
3. El usuario entra a "Mis dispositivos".
4. Ingresa `device_id` y codigo.
5. El backend valida datos.
6. Si todo esta bien, lo vincula al usuario.

Respuesta corta:

> "La vinculacion conecta el hardware fisico con la cuenta del usuario. Asi cada alerta puede quedar asociada a un conductor y a un ESP32."

---

## 14. Estado Online/Offline del ESP32

Rutas:

* `/api/panel/estado`
* `/api/devices/<device_id>/status`
* `/api/devices/<device_id>/presencia`
* `/api/esp32/heartbeat`

JS:

* `app/static/js/panel_vivo.js`

El estado se calcula combinando:

* WiFi: `last_seen`
* USB: `last_seen_usb`

Si pasan mas de 20 segundos sin senales, se considera offline.

Respuesta corta:

> "El sistema comprueba si el ESP32 esta vivo por WiFi y por USB. El dashboard consulta la API y actualiza el estado en vivo."

---

## 15. Seguridad en la comunicacion del ESP32

El ESP32 usa firma HMAC.

Funciones:

* `_device_payload`
* `_verify_device_signature`
* `_get_signed_device`

La firma usa:

* `device_secret`
* timestamp;
* nonce;
* datos del mensaje.

Protege contra:

* mensajes falsos;
* mensajes vencidos;
* repeticion de mensajes viejos;
* alteracion de datos.

Respuesta corta:

> "Usamos HMAC para comprobar que el mensaje realmente viene de un ESP32 registrado. El timestamp y nonce evitan ataques de repeticion."

---

## 16. Comandos del backend al ESP32

Modelo:

* `DispositivoComando`

Rutas:

* `/api/esp32/commands`
* `/api/devices/<device_id>/command`

Comandos permitidos:

* `alert_on`
* `alert_off`

Respuesta corta:

> "El ESP32 consulta al backend si tiene comandos pendientes. Si hay somnolencia recibe alert_on; cuando el conductor se recupera recibe alert_off."

---

## 17. Deteccion en tiempo real

Archivo:

* `deteccion_tiempo_real.py`

Usa:

* OpenCV;
* MediaPipe;
* TensorFlow/Keras;
* scaler de entrenamiento;
* EAR;
* pitch;
* suavizado temporal;
* tracker de ojos;
* tracker de cabeceo;
* grabacion de video;
* envio de reportes al backend;
* control del ESP32.

Respuesta corta:

> "La deteccion en tiempo real combina IA y reglas biometricas. No depende de una sola senal: usa modelo, ojos, cabeza, tiempo y suavizado."

---

## 18. Ojos cerrados mas rapidos

Umbrales actuales:

```python
EAR_CLOSED_THRESHOLD = 0.22
EAR_PARTIAL_THRESHOLD = 0.28
EYE_CLOSED_SECONDS_THRESHOLD = 0.50
EYE_PARTIAL_SECONDS_THRESHOLD = 1.20
NORMAL_BLINK_MAX_SECONDS = 0.30
```

Esto significa:

* un parpadeo normal no alerta;
* si el ojo queda cerrado mas de 0.50 segundos, alerta;
* si queda parcialmente cerrado mas de 1.20 segundos, alerta.

Respuesta corta:

> "Aceleramos la deteccion de ojos cerrados porque en conduccion no se puede esperar mucho. Si el cierre supera un parpadeo normal, el sistema alerta rapido."

---

## 19. Cabeceo menos sensible

Umbrales:

```python
HEAD_DROWSY_PROB_THRESHOLD = 0.78
HEAD_PITCH_DELTA_THRESHOLD = 18.0
HEAD_DROWSY_SECONDS_THRESHOLD = 1.20
HEAD_NEUTRAL_ALPHA = 0.04
```

Ahora el cabeceo requiere:

1. probabilidad alta del modelo;
2. diferencia real de pitch respecto de la postura neutral;
3. tiempo sostenido.

Respuesta corta:

> "Antes el cabeceo era muy sensible. Ahora el HeadNodTracker toma una postura neutral y exige probabilidad alta, movimiento real y tiempo sostenido."

---

## 20. Caja negra de video

Clases:

* `AsyncVideoWriter`
* `BlackBoxRecorder`
* `RecordedDrowsinessEvent`

Constantes:

```python
BLACKBOX_PRE_EVENT_SECONDS = 10.0
BLACKBOX_CONFIRMATION_SECONDS = 0.75
BLACKBOX_POST_EVENT_SECONDS = 10.0
```

La idea es grabar:

* segundos previos al evento;
* el evento;
* segundos posteriores.

Respuesta corta:

> "Funciona como caja negra. Guarda video de antes y despues de la alerta para tener evidencia del evento."

---

## 21. Reporte de detecciones al backend

Ruta:

* `/api/deteccion`

El script envia:

* usuario;
* tipo de evento;
* ruta del video;
* EAR;
* pitch;
* duracion;
* dispositivo.

El backend:

* valida la API key;
* guarda en MySQL si esta disponible;
* guarda en SQLite si MySQL no responde;
* actualiza score.

Respuesta corta:

> "La deteccion no queda solo en pantalla. Se reporta al backend y queda registrada con metricas y evidencia."

---

## 22. Mercado Pago y compra del dispositivo

Rutas:

* `/comprar`
* `/crear-preferencia`
* `/procesar-pago`
* `/pago/success`
* `/pago/failure`
* `/pago/pending`

Producto:

```python
PRODUCTO_VISION = {
    'title': 'Vision Device - Unidad Estandar',
    'quantity': 1,
    'unit_price': 189999.0,
    'currency_id': 'ARS',
}
```

Respuesta corta:

> "El proyecto tiene una vista de compra y rutas preparadas para flujo de pago con Mercado Pago."

---

## 23. Producto 3D y presentacion visual

Ruta:

* `/producto-3d`

Archivos:

* `app/templates/producto_3d.html`
* `app/static/models/vision_device.glb`
* `app/static/img/`

Respuesta corta:

> "Ademas del software, el proyecto muestra el producto fisico con imagenes y modelo 3D."

---

## 24. Archivos clave para nombrar en el examen

* `app/controllers.py`: rutas, validaciones, APIs, login, admin, dispositivos.
* `app/models.py`: tablas y relaciones.
* `app/services.py`: offline, sincronizacion, score.
* `app/templates/dashboard.html`: dashboard.
* `app/templates/admin_base_datos.html`: panel admin.
* `app/templates/editar_usuario.html`: modificacion de usuario.
* `app/templates/historial.html`: historial.
* `app/templates/dispositivos.html`: dispositivos.
* `app/static/js/panel_vivo.js`: estado vivo.
* `deteccion_tiempo_real.py`: IA, camara, ESP32 y reportes.
* `model/biometrics.py`: EAR y pitch.
* `model/data_collection.py`: normalizacion de landmarks.
* `esp32_alerta_wifi/esp32_alerta_wifi.ino`: firmware del ESP32.
* `vision_db_roles.sql`: roles.
* `device_linking.sql`: dispositivos.

---

## 25. Resumen final para decir completo en el oral

> "VISION es un sistema de seguridad vial inteligente. La camara analiza al conductor en tiempo real usando MediaPipe, TensorFlow, EAR y pitch. Si detecta ojos cerrados o cabeceo, activa una alarma fisica en un ESP32, graba evidencia tipo caja negra y envia el evento al backend Flask. La web permite registro, login, recuperacion de contrasena, dashboard, historial, dispositivos, modificacion de usuario y panel administrador. La base principal es MySQL, pero si falla se usa SQLite offline y luego se sincroniza. Ademas, el sistema tiene roles, API segura para ESP32 con HMAC, estado online/offline del dispositivo, score de conduccion y evidencias en video."
