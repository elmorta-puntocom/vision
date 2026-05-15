from flask import Flask, render_template, redirect, url_for, flash, request, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
import re, os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'vision-dev-key-change-in-prod')

# ── DB config ─────────────────────────────────────────────────────────────────
# MySQL (producción): descomenta y ajusta credenciales
# app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:password@127.0.0.1/vision_db'
# SQLite (desarrollo local, sin servidor):
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vision.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db      = SQLAlchemy(app)
bcrypt  = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Iniciá sesión para continuar.'
login_manager.login_message_category = 'warning'

# ── Models ────────────────────────────────────────────────────────────────────

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
    id             = db.Column(db.Integer, primary_key=True)
    usuario_id     = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    fecha_hora     = db.Column(db.DateTime, default=datetime.utcnow)
    tipo_evento    = db.Column(db.Enum('Ojos Cerrados', 'Cabeceo', 'Ambos'), nullable=False)
    video_path     = db.Column(db.String(255), nullable=False)
    valor_ear      = db.Column(db.Float)
    valor_pitch    = db.Column(db.Float)
    duracion_alerta = db.Column(db.Float)


class EstadisticaSeguridad(db.Model):
    __tablename__ = 'estadisticas_seguridad'
    id                  = db.Column(db.Integer, primary_key=True)
    usuario_id          = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    total_eventos       = db.Column(db.Integer, default=0)
    score_conduccion    = db.Column(db.Float, default=100.0)
    ultima_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@login_manager.user_loader
def load_user(uid):
    return Usuario.query.get(int(uid))

# ── Helpers ───────────────────────────────────────────────────────────────────

def valid_email(email):
    return re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email)

def valid_password(pw):
    if len(pw) < 8:           return False, "Mínimo 8 caracteres."
    if not re.search(r'[A-Z]', pw): return False, "Debe contener una mayúscula."
    if not re.search(r'\d', pw):    return False, "Debe contener un número."
    if not re.search(r'[!@#$%^&*(),.?\":{}|<>_\-]', pw):
        return False, "Debe contener un carácter especial."
    return True, ""

def _init_stats(usuario_id):
    if not EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first():
        db.session.add(EstadisticaSeguridad(usuario_id=usuario_id))
        db.session.commit()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        nombre   = request.form.get('nombre','').strip()
        apellido = request.form.get('apellido','').strip()
        email    = request.form.get('email','').strip().lower()
        pw       = request.form.get('password','')
        pw2      = request.form.get('confirm_password','')
        errors   = []
        if len(nombre)   < 2: errors.append("Nombre: mínimo 2 caracteres.")
        if len(apellido) < 2: errors.append("Apellido: mínimo 2 caracteres.")
        if not valid_email(email): errors.append("Correo inválido.")
        if Usuario.query.filter_by(email=email).first(): errors.append("El correo ya está registrado.")
        ok, msg = valid_password(pw)
        if not ok: errors.append(msg)
        if pw != pw2: errors.append("Las contraseñas no coinciden.")
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('register.html',
                                   form_data={'nombre':nombre,'apellido':apellido,'email':email})
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
        email    = request.form.get('email','').strip().lower()
        pw       = request.form.get('password','')
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
    return render_template('dashboard.html', stats=stats, detecciones=detecciones)


# Servir videos de forma privada (solo el dueño)
@app.route('/video/<int:det_id>')
@login_required
def serve_video(det_id):
    det = Deteccion.query.get_or_404(det_id)
    if det.usuario_id != current_user.id:
        abort(403)
    directory = os.path.dirname(det.video_path)
    filename  = os.path.basename(det.video_path)
    return send_from_directory(directory, filename)

@app.route('/alan-lopez')
def mi_pagina():
    return render_template('alan-lopez.html')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='127.0.0.1', port=5050)