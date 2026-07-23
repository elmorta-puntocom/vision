import threading
import time
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.pool import StaticPool

from . import db, logger
from .models import Deteccion, EstadisticaSeguridad


_offline_engine = None
_offline_meta = MetaData()
_sync_thread_started = False

detecciones_pending = Table(
    'detecciones_pending',
    _offline_meta,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('usuario_id', Integer, nullable=False),
    Column('fecha_hora', String(30), nullable=False),
    Column('tipo_evento', String(20), nullable=False),
    Column('video_path', String(255), nullable=False),
    Column('valor_ear', Float),
    Column('valor_pitch', Float),
    Column('duracion_alerta', Float),
    Column('sincronizado', Boolean, default=False),
    Column('mysql_id', Integer),
)

usuarios_cache = Table(
    'usuarios_cache',
    _offline_meta,
    Column('id', Integer, primary_key=True),
    Column('nombre', String(100), nullable=False),
    Column('apellido', String(100), nullable=False),
    Column('email', String(150), unique=True, nullable=False),
    Column('password_hash', String(255), nullable=False),
)


def init_offline_storage(app):
    global _offline_engine

    if _offline_engine is not None:
        return

    _offline_engine = create_engine(
        app.config['OFFLINE_SQLITE_URI'],
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    _offline_meta.create_all(_offline_engine)


def _engine():
    if _offline_engine is None:
        raise RuntimeError('La base offline SQLite no fue inicializada.')
    return _offline_engine


def cache_usuario(u):
    """Guarda o actualiza las credenciales del usuario en SQLite."""
    with _engine().connect() as conn:
        existe = conn.execute(
            usuarios_cache.select().where(usuarios_cache.c.email == u.email)
        ).fetchone()
        if existe:
            conn.execute(
                usuarios_cache.update()
                .where(usuarios_cache.c.email == u.email)
                .values(
                    password_hash=u.password_hash,
                    nombre=u.nombre,
                    apellido=u.apellido,
                )
            )
        else:
            conn.execute(
                usuarios_cache.insert().values(
                    id=u.id,
                    nombre=u.nombre,
                    apellido=u.apellido,
                    email=u.email,
                    password_hash=u.password_hash,
                )
            )
        conn.commit()


def guardar_deteccion_offline(
    usuario_id,
    tipo_evento,
    video_path,
    valor_ear=None,
    valor_pitch=None,
    duracion_alerta=None,
):
    """Guarda una detección en la cola SQLite local, con o sin WiFi."""
    with _engine().connect() as conn:
        conn.execute(
            detecciones_pending.insert().values(
                usuario_id=usuario_id,
                fecha_hora=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                tipo_evento=tipo_evento,
                video_path=video_path,
                valor_ear=valor_ear,
                valor_pitch=valor_pitch,
                duracion_alerta=duracion_alerta,
                sincronizado=False,
            )
        )
        conn.commit()
    logger.info(
        f'[OFFLINE] Detección guardada localmente -> usuario {usuario_id}, '
        f'evento: {tipo_evento}'
    )


def contar_pendientes_usuario(usuario_id):
    with _engine().connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(detecciones_pending)
            .where(detecciones_pending.c.usuario_id == usuario_id)
            .where(detecciones_pending.c.sincronizado == False)  # noqa: E712
        ).scalar_one()


def registrar_deteccion_mysql(
    usuario_id,
    tipo_evento,
    video_path,
    valor_ear=None,
    valor_pitch=None,
    duracion_alerta=None,
):
    """Guarda la deteccion en MySQL y actualiza estadisticas del usuario."""
    det = Deteccion(
        usuario_id=usuario_id,
        tipo_evento=tipo_evento,
        video_path=video_path,
        valor_ear=valor_ear,
        valor_pitch=valor_pitch,
        duracion_alerta=duracion_alerta,
    )
    db.session.add(det)
    db.session.flush()

    stats = EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first()
    if not stats:
        stats = EstadisticaSeguridad(usuario_id=usuario_id)
        db.session.add(stats)
        db.session.flush()

    stats.total_eventos = (stats.total_eventos or 0) + 1
    stats.score_conduccion = max(0.0, (stats.score_conduccion or 100.0) - 2.0)
    stats.ultima_actualizacion = datetime.utcnow()

    db.session.commit()
    logger.info(
        f'[MYSQL] Deteccion guardada -> usuario {usuario_id}, '
        f'evento: {tipo_evento}, id={det.id}'
    )
    return det, stats


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
    Migra las detecciones no sincronizadas de SQLite a MySQL y actualiza
    las estadísticas del conductor. Se ejecuta en un hilo separado.
    """
    with _engine().connect() as offline_conn:
        pendientes = offline_conn.execute(
            detecciones_pending.select().where(
                detecciones_pending.c.sincronizado == False  # noqa: E712
            )
        ).fetchall()

    if not pendientes:
        return

    logger.info(
        f'[SYNC] Intentando sincronizar {len(pendientes)} detección(es) pendiente(s)...'
    )

    if not mysql_disponible():
        logger.warning('[SYNC] MySQL no disponible. Se reintentará más tarde.')
        return

    sincronizadas = 0
    for row in pendientes:
        try:
            det, _ = registrar_deteccion_mysql(
                usuario_id=row.usuario_id,
                tipo_evento=row.tipo_evento,
                video_path=row.video_path,
                valor_ear=row.valor_ear,
                valor_pitch=row.valor_pitch,
                duracion_alerta=row.duracion_alerta,
            )

            with _engine().connect() as offline_conn:
                offline_conn.execute(
                    detecciones_pending.update()
                    .where(detecciones_pending.c.id == row.id)
                    .values(sincronizado=True, mysql_id=det.id)
                )
                offline_conn.commit()

            sincronizadas += 1
            logger.info(
                f'[SYNC] Detección offline id={row.id} -> MySQL id={det.id}'
            )

        except Exception as e:
            db.session.rollback()
            logger.error(f'[SYNC] Error al sincronizar id={row.id}: {e}')

    if sincronizadas:
        logger.info(
            '[SYNC] Sincronización completada: '
            f'{sincronizadas}/{len(pendientes)} detección(es).'
        )


def _hilo_sincronizador(app):
    """Hilo daemon que intenta sincronizar cada 60 segundos."""
    time.sleep(10)
    while True:
        try:
            with app.app_context():
                sincronizar_pendientes()
        except Exception as e:
            logger.error(f'[SYNC] Error inesperado en hilo sincronizador: {e}')
        time.sleep(60)


def start_sync_thread(app):
    global _sync_thread_started

    if _sync_thread_started:
        return None

    hilo = threading.Thread(
        target=_hilo_sincronizador,
        args=(app,),
        daemon=True,
        name='SyncThread',
    )
    hilo.start()
    _sync_thread_started = True
    logger.info('[SYNC] Hilo sincronizador iniciado (intervalo: 60s).')
    return hilo
