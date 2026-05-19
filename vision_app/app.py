from flask import Flask, render_template, redirect, url_for, flash, request, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
from sqlalchemy import text
import threading, time, re, os, logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'vision-dev-key-change-in-prod')

# ── Configuración MySQL (fuente de verdad, requiere WiFi) ─────────────────────
MYSQL_USER     = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
MYSQL_HOST     = os.environ.get('MYSQL_HOST', '127.0.0.1')
MYSQL_PORT     = os.environ.get('MYSQL_PORT', '3306')
MYSQL_DB       = os.environ.get('MYSQL_DB', 'vision_db')

MYSQL_URI = (
    f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}'
    f'@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4'
)

# ── Configuración SQLite (cola offline, siempre disponible) ───────────────────
SQLITE_URI = 'sqlite:///vision_offline.db'

# Flask-SQLAlchemy usa la URI principal (MySQL). Si no hay conexión, el
# sistema igual arranca porque la cola offline es independiente (SQLite puro).
app.config['SQLALCHEMY_DATABASE_URI']        = MYSQL_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Pool que no muere si MySQL no está disponible al inicio
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,          # detecta conexiones muertas antes de usarlas
    'pool_recycle':  300,           # recicla conexiones cada 5 min
    'connect_args':  {'connect_timeout': 5},
}

db     = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view         = 'login'
login_manager.login_message      = 'Iniciá sesión para continuar.'
login_manager.login_message_category = 'warning'

# ── Modelos MySQL (fuente de verdad) ──────────────────────────────────────────

class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuarios'
    id             = db.Column(db.Integer, primary_key=True)
    nombre         = db.Column(db.String(100), nullable=False)
    apellido       = db.Column(db.String(100), nullable=False)
    email          = db.Column(db.String(150), unique=True, nullable=False)
    password_hash  = db.Column(db.String(255), nullable=False)
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)

    detecciones  = db.relationship('Deteccion', backref='usuario', lazy=True,
                                   cascade='all, delete-orphan')
    estadisticas = db.relationship('EstadisticaSeguridad', backref='usuario',
                                   uselist=False, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = bcrypt.generate_password_hash(pw).decode('utf-8')

    def check_password(self, pw):
        return bcrypt.check_password_hash(self.password_hash, pw)


class Deteccion(db.Model):
    __tablename__ = 'detecciones'
    id              = db.Column(db.Integer, primary_key=True)
    usuario_id      = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    fecha_hora      = db.Column(db.DateTime, default=datetime.utcnow)
    tipo_evento     = db.Column(db.Enum('Ojos Cerrados', 'Cabeceo', 'Ambos'), nullable=False)
    video_path      = db.Column(db.String(255), nullable=False)
    valor_ear       = db.Column(db.Float)
    valor_pitch     = db.Column(db.Float)
    duracion_alerta = db.Column(db.Float)


class EstadisticaSeguridad(db.Model):
    __tablename__ = 'estadisticas_seguridad'
    id                   = db.Column(db.Integer, primary_key=True)
    usuario_id           = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    total_eventos        = db.Column(db.Integer, default=0)
    score_conduccion     = db.Column(db.Float, default=100.0)
    ultima_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@login_manager.user_loader
def load_user(uid):
    try:
        return Usuario.query.get(int(uid))
    except Exception:
        return None

# ── Cola offline (SQLite puro, sin Flask-SQLAlchemy) ─────────────────────────
# Se usa sqlalchemy core directamente para no mezclar sesiones.

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, MetaData, Table
from sqlalchemy.pool import StaticPool

_offline_engine = create_engine(
    SQLITE_URI,
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)
_offline_meta = MetaData()

# Tabla de detecciones pendientes de sincronizar con MySQL
detecciones_pending = Table(
    'detecciones_pending', _offline_meta,
    Column('id',              Integer, primary_key=True, autoincrement=True),
    Column('usuario_id',      Integer, nullable=False),
    Column('fecha_hora',      String(30), nullable=False),
    Column('tipo_evento',     String(20), nullable=False),
    Column('video_path',      String(255), nullable=False),
    Column('valor_ear',       Float),
    Column('valor_pitch',     Float),
    Column('duracion_alerta', Float),
    Column('sincronizado',    Boolean, default=False),
    Column('mysql_id',        Integer),   # id asignado por MySQL tras sync
)

_offline_meta.create_all(_offline_engine)


def guardar_deteccion_offline(usuario_id, tipo_evento, video_path,
                               valor_ear=None, valor_pitch=None, duracion_alerta=None):
    """
    Guarda una detección en la cola SQLite local.
    Siempre funciona, con o sin WiFi.
    """
    with _offline_engine.connect() as conn:
        conn.execute(detecciones_pending.insert().values(
            usuario_id      = usuario_id,
            fecha_hora      = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            tipo_evento     = tipo_evento,
            video_path      = video_path,
            valor_ear       = valor_ear,
            valor_pitch     = valor_pitch,
            duracion_alerta = duracion_alerta,
            sincronizado    = False,
        ))
        conn.commit()
    logger.info(f'[OFFLINE] Detección guardada localmente → usuario {usuario_id}, evento: {tipo_evento}')


# ── Sincronizador automático ──────────────────────────────────────────────────

def mysql_disponible():
    """Devuelve True si MySQL responde."""
    try:
        with db.engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        return True
    except Exception:
        return False


def sincronizar_pendientes():
    """
    Migra las detecciones no sincronizadas de SQLite → MySQL y actualiza
    las estadísticas del conductor. Se ejecuta en un hilo separado.
    """
    with _offline_engine.connect() as offline_conn:
        pendientes = offline_conn.execute(
            detecciones_pending.select().where(
                detecciones_pending.c.sincronizado == False   # noqa: E712
            )
        ).fetchall()

    if not pendientes:
        return

    logger.info(f'[SYNC] Intentando sincronizar {len(pendientes)} detección(es) pendiente(s)...')

    if not mysql_disponible():
        logger.warning('[SYNC] MySQL no disponible. Se reintentará más tarde.')
        return

    sincronizadas = 0
    with app.app_context():
        for row in pendientes:
            try:
                det = Deteccion(
                    usuario_id      = row.usuario_id,
                    fecha_hora      = datetime.strptime(row.fecha_hora, '%Y-%m-%d %H:%M:%S'),
                    tipo_evento     = row.tipo_evento,
                    video_path      = row.video_path,
                    valor_ear       = row.valor_ear,
                    valor_pitch     = row.valor_pitch,
                    duracion_alerta = row.duracion_alerta,
                )
                db.session.add(det)
                db.session.flush()   # obtiene el id generado por MySQL

                # Actualiza estadísticas del conductor
                stats = EstadisticaSeguridad.query.filter_by(usuario_id=row.usuario_id).first()
                if stats:
                    stats.total_eventos += 1
                    # Penalización de score: -2 por evento, mínimo 0
                    stats.score_conduccion = max(0.0, stats.score_conduccion - 2.0)

                db.session.commit()

                # Marca como sincronizado en SQLite
                with _offline_engine.connect() as offline_conn:
                    offline_conn.execute(
                        detecciones_pending.update()
                        .where(detecciones_pending.c.id == row.id)
                        .values(sincronizado=True, mysql_id=det.id)
                    )
                    offline_conn.commit()

                sincronizadas += 1
                logger.info(f'[SYNC] ✓ Detección offline id={row.id} → MySQL id={det.id}')

            except Exception as e:
                db.session.rollback()
                logger.error(f'[SYNC] ✗ Error al sincronizar id={row.id}: {e}')

    if sincronizadas:
        logger.info(f'[SYNC] Sincronización completada: {sincronizadas}/{len(pendientes)} detección(es).')


def _hilo_sincronizador():
    """Hilo daemon que intenta sincronizar cada 60 segundos."""
    # Espera inicial para que Flask termine de arrancar
    time.sleep(10)
    while True:
        try:
            sincronizar_pendientes()
        except Exception as e:
            logger.error(f'[SYNC] Error inesperado en hilo sincronizador: {e}')
        time.sleep(60)


# ── Helpers ───────────────────────────────────────────────────────────────────

def valid_email(email):
    return re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email)

def valid_password(pw):
    if len(pw) < 8:                   return False, "Mínimo 8 caracteres."
    if not re.search(r'[A-Z]', pw):   return False, "Debe contener una mayúscula."
    if not re.search(r'\d', pw):      return False, "Debe contener un número."
    if not re.search(r'[!@#$%^&*(),.?\":{}|<>_\-]', pw):
        return False, "Debe contener un carácter especial."
    return True, ""

def _init_stats(usuario_id):
    if not EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first():
        db.session.add(EstadisticaSeguridad(usuario_id=usuario_id))
        db.session.commit()

# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        nombre   = request.form.get('nombre',   '').strip()
        apellido = request.form.get('apellido', '').strip()
        email    = request.form.get('email',    '').strip().lower()
        pw       = request.form.get('password', '')
        pw2      = request.form.get('confirm_password', '')
        errors   = []

        if len(nombre)   < 2: errors.append("Nombre: mínimo 2 caracteres.")
        if len(apellido) < 2: errors.append("Apellido: mínimo 2 caracteres.")
        if not valid_email(email): errors.append("Correo inválido.")
        if Usuario.query.filter_by(email=email).first():
            errors.append("El correo ya está registrado.")
        ok, msg = valid_password(pw)
        if not ok: errors.append(msg)
        if pw != pw2: errors.append("Las contraseñas no coinciden.")

        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('register.html',
                                   form_data={'nombre': nombre, 'apellido': apellido, 'email': email})

        u = Usuario(nombre=nombre, apellido=apellido, email=email)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
        _init_stats(u.id)
        flash('¡Cuenta creada! Podés iniciar sesión.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', form_data={})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email',    '').strip().lower()
        pw       = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'
        u = Usuario.query.filter_by(email=email).first()
        if u and u.check_password(pw):
            login_user(u, remember=remember)
            flash(f'¡Bienvenido/a, {u.nombre}!', 'success')
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Correo o contraseña incorrectos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    _init_stats(current_user.id)
    stats = EstadisticaSeguridad.query.filter_by(usuario_id=current_user.id).first()
    detecciones = (Deteccion.query
                   .filter_by(usuario_id=current_user.id)
                   .order_by(Deteccion.fecha_hora.desc())
                   .limit(20).all())

    # Conteo de detecciones pendientes de sincronizar (para mostrar aviso en UI)
    with _offline_engine.connect() as conn:
        pendientes_count = conn.execute(
            detecciones_pending.select()
            .where(detecciones_pending.c.usuario_id == current_user.id)
            .where(detecciones_pending.c.sincronizado == False)   # noqa: E712
        ).rowcount

    return render_template('dashboard.html',
                           stats=stats,
                           detecciones=detecciones,
                           pendientes_count=pendientes_count)


@app.route('/video/<int:det_id>')
@login_required
def serve_video(det_id):
    det = Deteccion.query.get_or_404(det_id)
    if det.usuario_id != current_user.id:
        abort(403)
    directory = os.path.dirname(det.video_path)
    filename  = os.path.basename(det.video_path)
    return send_from_directory(directory, filename)


# ── Endpoint interno para el módulo de detección (llamado por el script Python) ──
# El script de detección llama a este endpoint cuando detecta somnolencia.
# Guarda siempre en SQLite primero; el hilo sincronizador lo sube a MySQL.

@app.route('/api/deteccion', methods=['POST'])
def api_deteccion():
    """
    Endpoint interno (no expuesto a internet) para registrar detecciones
    desde el script de visión artificial.

    Espera JSON:
    {
        "usuario_id":      int,
        "tipo_evento":     "Ojos Cerrados" | "Cabeceo" | "Ambos",
        "video_path":      "ruta/al/video.mp4",
        "valor_ear":       float | null,
        "valor_pitch":     float | null,
        "duracion_alerta": float | null,
        "api_key":         "clave-interna"
    }
    """
    API_KEY_INTERNA = os.environ.get('VISION_API_KEY', 'vision-internal-key')
    data = request.get_json(silent=True)

    if not data or data.get('api_key') != API_KEY_INTERNA:
        abort(401)

    try:
        guardar_deteccion_offline(
            usuario_id      = data['usuario_id'],
            tipo_evento     = data['tipo_evento'],
            video_path      = data['video_path'],
            valor_ear       = data.get('valor_ear'),
            valor_pitch     = data.get('valor_pitch'),
            duracion_alerta = data.get('duracion_alerta'),
        )
        return {'status': 'ok', 'message': 'Detección guardada localmente.'}, 201
    except Exception as e:
        logger.error(f'[API] Error al guardar detección: {e}')
        return {'status': 'error', 'message': str(e)}, 500


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        # Crea las tablas en MySQL si no existen
        try:
            db.create_all()
            logger.info('[DB] Tablas MySQL verificadas/creadas.')
        except Exception as e:
            logger.error(f'[DB] No se pudo conectar a MySQL al inicio: {e}')
            logger.warning('[DB] El servidor arrancará de todas formas. '
                           'Las detecciones se guardarán offline hasta que MySQL esté disponible.')

    # Inicia el hilo de sincronización en segundo plano
    hilo = threading.Thread(target=_hilo_sincronizador, daemon=True, name='SyncThread')
    hilo.start()
    logger.info('[SYNC] Hilo sincronizador iniciado (intervalo: 60s).')

    app.run(debug=True, host='127.0.0.1', port=5050)