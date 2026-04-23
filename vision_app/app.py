from flask import Flask, render_template, redirect, url_for, flash, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
import re
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vision.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Iniciá sesión para acceder a esta página.'
login_manager.login_message_category = 'warning'


# ─── Models ───────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    apellido = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Validation helpers ───────────────────────────────────────────────────────

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_password(password):
    """At least 8 chars, one uppercase, one digit, one special char."""
    if len(password) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."
    if not re.search(r'[A-Z]', password):
        return False, "La contraseña debe contener al menos una letra mayúscula."
    if not re.search(r'\d', password):
        return False, "La contraseña debe contener al menos un número."
    if not re.search(r'[!@#$%^&*(),.?\":{}|<>_\-]', password):
        return False, "La contraseña debe contener al menos un carácter especial."
    return True, ""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        apellido = request.form.get('apellido', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        errors = []

        if not nombre or len(nombre) < 2:
            errors.append("El nombre debe tener al menos 2 caracteres.")
        if not apellido or len(apellido) < 2:
            errors.append("El apellido debe tener al menos 2 caracteres.")
        if not validate_email(email):
            errors.append("El correo electrónico no es válido.")
        if User.query.filter_by(email=email).first():
            errors.append("Ya existe una cuenta con ese correo electrónico.")

        pw_valid, pw_msg = validate_password(password)
        if not pw_valid:
            errors.append(pw_msg)
        if password != confirm:
            errors.append("Las contraseñas no coinciden.")

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('register.html', form_data={
                'nombre': nombre, 'apellido': apellido, 'email': email
            })

        user = User(nombre=nombre, apellido=apellido, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('¡Cuenta creada exitosamente! Podés iniciar sesión ahora.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', form_data={})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            flash(f'¡Bienvenido/a, {user.nombre}!', 'success')
            return redirect(next_page or url_for('dashboard'))
        else:
            flash('Correo o contraseña incorrectos.', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5050)
