import os
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from . import db, logger
from .models import Deteccion, EstadisticaSeguridad, Usuario
from .services import contar_pendientes_usuario, guardar_deteccion_offline


bp = Blueprint('main', __name__)


def valid_email(email):
    return re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email)


def valid_password(pw):
    if len(pw) < 8:
        return False, 'Mínimo 8 caracteres.'
    if not re.search(r'[A-Z]', pw):
        return False, 'Debe contener una mayúscula.'
    if not re.search(r'\d', pw):
        return False, 'Debe contener un número.'
    if not re.search(r'[!@#$%^&*(),.?\":{}|<>_\-]', pw):
        return False, 'Debe contener un carácter especial.'
    return True, ''


def _init_stats(usuario_id):
    if not EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first():
        db.session.add(EstadisticaSeguridad(usuario_id=usuario_id))
        db.session.commit()


@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        apellido = request.form.get('apellido', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        errors = []

        if len(nombre) < 2:
            errors.append('Nombre: mínimo 2 caracteres.')
        if len(apellido) < 2:
            errors.append('Apellido: mínimo 2 caracteres.')
        if not valid_email(email):
            errors.append('Correo inválido.')
        if Usuario.query.filter_by(email=email).first():
            errors.append('El correo ya está registrado.')
        ok, msg = valid_password(pw)
        if not ok:
            errors.append(msg)
        if pw != pw2:
            errors.append('Las contraseñas no coinciden.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template(
                'register.html',
                form_data={'nombre': nombre, 'apellido': apellido, 'email': email},
            )

        u = Usuario(nombre=nombre, apellido=apellido, email=email)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
        _init_stats(u.id)
        flash('¡Cuenta creada! Podés iniciar sesión.', 'success')
        return redirect(url_for('main.login'))

    return render_template('register.html', form_data={})


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'
        u = Usuario.query.filter_by(email=email).first()

        if u and u.check_password(pw):
            login_user(u, remember=remember)
            flash(f'¡Bienvenido/a, {u.nombre}!', 'success')
            return redirect(request.args.get('next') or url_for('main.dashboard'))

        flash('Correo o contraseña incorrectos.', 'danger')

    return render_template('login.html')


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.', 'info')
    return redirect(url_for('main.index'))


@bp.route('/dashboard')
@login_required
def dashboard():
    _init_stats(current_user.id)
    stats = EstadisticaSeguridad.query.filter_by(usuario_id=current_user.id).first()
    detecciones = (
        Deteccion.query
        .filter_by(usuario_id=current_user.id)
        .order_by(Deteccion.fecha_hora.desc())
        .limit(20)
        .all()
    )
    pendientes_count = contar_pendientes_usuario(current_user.id)

    return render_template(
        'dashboard.html',
        stats=stats,
        detecciones=detecciones,
        pendientes_count=pendientes_count,
    )


@bp.route('/video/<int:det_id>')
@login_required
def serve_video(det_id):
    det = Deteccion.query.get_or_404(det_id)
    if det.usuario_id != current_user.id:
        abort(403)

    directory = os.path.dirname(det.video_path)
    filename = os.path.basename(det.video_path)
    return send_from_directory(directory, filename)


@bp.route('/api/deteccion', methods=['POST'])
def api_deteccion():
    """
    Endpoint interno para registrar detecciones desde el script de visión artificial.
    """
    data = request.get_json(silent=True)

    if not data or data.get('api_key') != current_app.config['VISION_API_KEY']:
        abort(401)

    try:
        guardar_deteccion_offline(
            usuario_id=data['usuario_id'],
            tipo_evento=data['tipo_evento'],
            video_path=data['video_path'],
            valor_ear=data.get('valor_ear'),
            valor_pitch=data.get('valor_pitch'),
            duracion_alerta=data.get('duracion_alerta'),
        )
        return {'status': 'ok', 'message': 'Detección guardada localmente.'}, 201
    except Exception as e:
        logger.error(f'[API] Error al guardar detección: {e}')
        return {'status': 'error', 'message': str(e)}, 500
