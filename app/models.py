from datetime import datetime

from flask_login import UserMixin

from . import bcrypt, db


usuario_roles = db.Table(
    'usuario_roles',
    db.Column(
        'usuario_id',
        db.Integer,
        db.ForeignKey('usuarios.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    db.Column(
        'rol_id',
        db.Integer,
        db.ForeignKey('roles.id', ondelete='CASCADE'),
        primary_key=True,
    ),
)


class Rol(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False, unique=True)


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    apellido = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)

    detecciones = db.relationship(
        'Deteccion',
        backref='usuario',
        lazy=True,
        cascade='all, delete-orphan',
    )
    estadisticas = db.relationship(
        'EstadisticaSeguridad',
        backref='usuario',
        uselist=False,
        cascade='all, delete-orphan',
    )
    roles = db.relationship(
        'Rol',
        secondary=usuario_roles,
        backref=db.backref('usuarios', lazy='dynamic'),
        lazy='select',
    )

    def set_password(self, pw):
        self.password_hash = bcrypt.generate_password_hash(pw).decode('utf-8')

    def check_password(self, pw):
        return bcrypt.check_password_hash(self.password_hash, pw)

    def has_role(self, nombre):
        return any(rol.nombre == nombre for rol in self.roles)


class Deteccion(db.Model):
    __tablename__ = 'detecciones'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer,
        db.ForeignKey('usuarios.id', ondelete='CASCADE'),
        nullable=False,
    )
    fecha_hora = db.Column(db.DateTime, default=datetime.utcnow)
    tipo_evento = db.Column(db.Enum('Ojos Cerrados', 'Cabeceo', 'Ambos'), nullable=False)
    video_path = db.Column(db.String(255), nullable=False)
    valor_ear = db.Column(db.Float)
    valor_pitch = db.Column(db.Float)
    duracion_alerta = db.Column(db.Float)


class EstadisticaSeguridad(db.Model):
    __tablename__ = 'estadisticas_seguridad'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer,
        db.ForeignKey('usuarios.id', ondelete='CASCADE'),
        nullable=False,
    )
    total_eventos = db.Column(db.Integer, default=0)
    score_conduccion = db.Column(db.Float, default=100.0)
    ultima_actualizacion = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
