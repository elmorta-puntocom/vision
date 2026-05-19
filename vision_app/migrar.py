# migrar.py — ejecutar con: python migrar.py
import sqlite3
from app import app, db, Usuario, Deteccion, EstadisticaSeguridad, bcrypt

SQLITE_VIEJO = 'instance/vision.db'  # ajustá la ruta si es diferente

with app.app_context():
    conn = sqlite3.connect(SQLITE_VIEJO)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Usuarios
    cur.execute("SELECT * FROM usuarios")
    for row in cur.fetchall():
        if not Usuario.query.filter_by(email=row['email']).first():
            u = Usuario(
                id             = row['id'],
                nombre         = row['nombre'],
                apellido       = row['apellido'],
                email          = row['email'],
                password_hash  = row['password_hash'],
                fecha_registro = row['fecha_registro'],
            )
            db.session.add(u)
    db.session.commit()
    print("✓ Usuarios migrados")

    # Estadísticas
    cur.execute("SELECT * FROM estadisticas_seguridad")
    for row in cur.fetchall():
        if not EstadisticaSeguridad.query.filter_by(usuario_id=row['usuario_id']).first():
            db.session.add(EstadisticaSeguridad(
                usuario_id       = row['usuario_id'],
                total_eventos    = row['total_eventos'],
                score_conduccion = row['score_conduccion'],
            ))
    db.session.commit()
    print("✓ Estadísticas migradas")

    # Detecciones
    cur.execute("SELECT * FROM detecciones")
    for row in cur.fetchall():
        db.session.add(Deteccion(
            usuario_id      = row['usuario_id'],
            fecha_hora      = row['fecha_hora'],
            tipo_evento     = row['tipo_evento'],
            video_path      = row['video_path'],
            valor_ear       = row['valor_ear'],
            valor_pitch     = row['valor_pitch'],
            duracion_alerta = row['duracion_alerta'],
        ))
    db.session.commit()
    print("✓ Detecciones migradas")

    conn.close()
    print("Migración completa.")