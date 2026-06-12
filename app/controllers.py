import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_mail import Message
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import bcrypt, db, logger, mail
from .models import (
    Deteccion,
    Dispositivo,
    DispositivoComando,
    DispositivoEvento,
    EstadisticaSeguridad,
    Rol,
    Usuario,
)
from .services import (
    contar_pendientes_usuario,
    guardar_deteccion_offline,
    mysql_disponible,
)


bp = Blueprint('main', __name__)

DEFAULT_USER_ROLE = 'Usuario común'
DEFAULT_ROLES = ('Administrador', DEFAULT_USER_ROLE)
DEVICE_SIGNATURE_TTL_SECONDS = 300
DEVICE_ONLINE_WINDOW_MINUTES = 10
ALLOWED_DEVICE_COMMANDS = {'alert_on', 'alert_off'}


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


def valid_optional_password(pw):
    return not pw or len(pw) >= 6


def split_full_name(full_name):
    parts = full_name.strip().split()
    if len(parts) < 2:
        return '', ''
    return parts[0], ' '.join(parts[1:])


def _ensure_default_roles():
    db.create_all()
    created = False
    for role_name in DEFAULT_ROLES:
        if not Rol.query.filter_by(nombre=role_name).first():
            db.session.add(Rol(nombre=role_name))
            created = True
    if created:
        db.session.commit()


def _default_user_role():
    _ensure_default_roles()
    return Rol.query.filter_by(nombre=DEFAULT_USER_ROLE).first()


def _ensure_user_role(usuario):
    if usuario.roles:
        return

    default_role = _default_user_role()
    if default_role:
        usuario.roles.append(default_role)
        db.session.commit()


def _init_stats(usuario_id):
    if not EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first():
        db.session.add(EstadisticaSeguridad(usuario_id=usuario_id))
        db.session.commit()


def _clean_device_id(value):
    return (value or '').strip().upper()


def _device_payload(data, include_mac=False):
    parts = [
        _clean_device_id(data.get('device_id')),
        str(data.get('ts', '')).strip(),
        str(data.get('nonce', '')).strip(),
    ]
    if include_mac:
        parts.insert(1, str(data.get('mac', '')).strip().upper())
    return '|'.join(parts)


def _verify_device_signature(data, dispositivo, include_mac=False):
    signature = str(data.get('signature', '')).strip().lower()
    ts = str(data.get('ts', '')).strip()
    nonce = str(data.get('nonce', '')).strip()

    if not signature or not ts or not nonce:
        return False, 'missing_signature_data'

    try:
        timestamp = int(ts)
    except ValueError:
        return False, 'invalid_timestamp'

    if abs(time.time() - timestamp) > DEVICE_SIGNATURE_TTL_SECONDS:
        return False, 'expired_timestamp'

    if dispositivo.last_nonce and dispositivo.last_nonce == nonce:
        return False, 'repeated_nonce'

    expected = hmac.new(
        dispositivo.device_secret.encode('utf-8'),
        _device_payload(data, include_mac=include_mac).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return False, 'invalid_signature'

    return True, ''


def _get_signed_device(data, include_mac=False):
    device_id = _clean_device_id(data.get('device_id'))
    dispositivo = Dispositivo.query.filter_by(device_id=device_id).first()

    if not dispositivo:
        return None, 'device_not_registered'

    ok, error = _verify_device_signature(data, dispositivo, include_mac=include_mac)
    if not ok:
        return None, error

    return dispositivo, ''


@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/comprar')
def comprar():
    return render_template('comprar.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if not mysql_disponible():
        logger.warning('[REGISTER] Intento de registro bloqueado: MySQL offline.')
        return redirect(url_for('main.register_offline'))

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

        try:
            if Usuario.query.filter_by(email=email).first():
                flash('El correo ya está registrado.', 'danger')
                return render_template(
                    'register.html',
                    form_data={'nombre': nombre, 'apellido': apellido, 'email': email},
                )

            default_role = _default_user_role()
            u = Usuario(nombre=nombre, apellido=apellido, email=email)
            u.set_password(pw)
            if default_role:
                u.roles.append(default_role)
            db.session.add(u)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning(f'[REGISTER] Error al crear usuario durante el registro: {exc}')
            if not mysql_disponible():
                return redirect(url_for('main.register_offline'))
            raise
        _init_stats(u.id)
        flash('¡Cuenta creada! Podés iniciar sesión.', 'success')
        return redirect(url_for('main.login'))

    return render_template('register.html', form_data={})


@bp.route('/register/offline')
def register_offline():
    if mysql_disponible():
        return redirect(url_for('main.register'))
    return render_template('register_offline.html')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        if not mysql_disponible():
            logger.warning('[LOGIN] Intento de inicio de sesion bloqueado: MySQL offline.')
            return redirect(url_for('main.login_offline'))

        try:
            u = Usuario.query.filter_by(email=email).first()
        except Exception as exc:
            logger.warning(f'[LOGIN] Error al consultar usuarios durante el login: {exc}')
            if not mysql_disponible():
                return redirect(url_for('main.login_offline'))
            raise

        if u and u.check_password(pw):
            login_user(u, remember=remember)
            flash(f'¡Bienvenido/a, {u.nombre}!', 'success')
            return redirect(request.args.get('next') or url_for('main.dashboard'))

        flash('Correo o contraseña incorrectos.', 'danger')

    return render_template('login.html')


@bp.route('/login/offline')
def login_offline():
    if mysql_disponible():
        return redirect(url_for('main.login'))
    return render_template('login_offline.html')


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.', 'info')
    return redirect(url_for('main.index'))


@bp.route('/dashboard')
@login_required
def dashboard():
    _ensure_default_roles()
    _ensure_user_role(current_user)
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
    dispositivos_vinculados = (
        Dispositivo.query
        .filter_by(usuario_id=current_user.id)
        .order_by(Dispositivo.linked_at.desc())
        .all()
    )

    return render_template(
        'dashboard.html',
        stats=stats,
        detecciones=detecciones,
        pendientes_count=pendientes_count,
        dispositivos=dispositivos_vinculados,
    )


@bp.route('/dispositivos', methods=['GET', 'POST'])
@login_required
def dispositivos():
    if request.method == 'POST':
        device_id = _clean_device_id(request.form.get('device_id'))
        activation_code = request.form.get('activation_code', '').strip()

        dispositivo = Dispositivo.query.filter_by(device_id=device_id).first()
        if not dispositivo:
            flash('Dispositivo no registrado.', 'danger')
            return redirect(url_for('main.dispositivos'))

        if dispositivo.usuario_id:
            flash('Ese dispositivo ya esta vinculado a una cuenta.', 'danger')
            return redirect(url_for('main.dispositivos'))

        if not dispositivo.last_seen:
            flash('Primero encende el ESP32 para que se registre en el servidor.', 'warning')
            return redirect(url_for('main.dispositivos'))

        online_limit = datetime.utcnow() - timedelta(minutes=DEVICE_ONLINE_WINDOW_MINUTES)
        if dispositivo.last_seen < online_limit:
            flash('El ESP32 no se conecto recientemente. Encendelo y volve a intentar.', 'warning')
            return redirect(url_for('main.dispositivos'))

        if not bcrypt.check_password_hash(dispositivo.activation_code_hash, activation_code):
            flash('Codigo de activacion incorrecto.', 'danger')
            return redirect(url_for('main.dispositivos'))

        dispositivo.usuario_id = current_user.id
        dispositivo.linked_at = datetime.utcnow()
        db.session.commit()
        flash('Dispositivo vinculado correctamente.', 'success')
        return redirect(url_for('main.dispositivos'))

    vinculados = (
        Dispositivo.query
        .filter_by(usuario_id=current_user.id)
        .order_by(Dispositivo.linked_at.desc())
        .all()
    )
    return render_template(
        'dispositivos.html',
        dispositivos=vinculados,
        now=datetime.utcnow,
    )


@bp.route('/admin/base-datos')
@login_required
def admin_base_datos():
    _ensure_default_roles()

    if not current_user.has_role('Administrador'):
        flash('No tenés permisos para acceder al panel de administración.', 'danger')
        return redirect(url_for('main.dashboard'))

    usuarios = Usuario.query.order_by(Usuario.fecha_registro.desc()).all()
    detecciones_por_usuario = dict(
        db.session.query(Deteccion.usuario_id, db.func.count(Deteccion.id))
        .group_by(Deteccion.usuario_id)
        .all()
    )
    estadisticas_por_usuario = {
        stat.usuario_id: stat for stat in EstadisticaSeguridad.query.all()
    }
    ultimas_detecciones = (
        Deteccion.query
        .order_by(Deteccion.fecha_hora.desc())
        .limit(12)
        .all()
    )

    usuarios_resumen = []
    for usuario in usuarios:
        stats_usuario = estadisticas_por_usuario.get(usuario.id)
        usuarios_resumen.append({
            'usuario': usuario,
            'roles': ', '.join(rol.nombre for rol in usuario.roles) or 'Sin rol',
            'detecciones': detecciones_por_usuario.get(usuario.id, 0),
            'score': stats_usuario.score_conduccion if stats_usuario else 100.0,
            'ultima_actualizacion': (
                stats_usuario.ultima_actualizacion if stats_usuario else None
            ),
        })

    total_usuarios = len(usuarios)
    total_detecciones = sum(detecciones_por_usuario.values())
    score_promedio = (
        db.session.query(db.func.avg(EstadisticaSeguridad.score_conduccion)).scalar()
        or 100.0
    )

    return render_template(
        'admin_base_datos.html',
        usuarios_resumen=usuarios_resumen,
        ultimas_detecciones=ultimas_detecciones,
        total_usuarios=total_usuarios,
        total_detecciones=total_detecciones,
        score_promedio=score_promedio,
    )


@bp.route('/usuarios/<int:user_id>/editar', methods=['GET', 'POST'])
@login_required
def editar_usuario(user_id):
    _ensure_default_roles()
    is_admin = current_user.has_role('Administrador')
    if not is_admin and user_id != current_user.id:
        abort(403)

    usuario = Usuario.query.get_or_404(user_id)
    _ensure_user_role(usuario)
    roles_disponibles = Rol.query.order_by(Rol.nombre.asc()).all()
    errors = {}

    if request.method == 'POST':
        nombre_apellido = request.form.get('nombre_apellido', '').strip()
        email = request.form.get('email', '').strip().lower() if is_admin else usuario.email
        nombre, apellido = split_full_name(nombre_apellido)

        if not nombre_apellido:
            errors['nombre_apellido'] = 'El nombre y apellido son obligatorios.'
        elif not nombre or not apellido:
            errors['nombre_apellido'] = 'Ingresá nombre y apellido.'

        if is_admin:
            if not email:
                errors['email'] = 'El e-mail es obligatorio.'
            elif not valid_email(email):
                errors['email'] = 'Ingresá un e-mail válido.'
            else:
                existing_email = Usuario.query.filter_by(email=email).first()
                if existing_email and existing_email.id != usuario.id:
                    errors['email'] = 'Ese e-mail ya está registrado.'

        if is_admin:
            roles_seleccionados = request.form.getlist('roles')
            selected_roles = Rol.query.filter(Rol.nombre.in_(roles_seleccionados)).all()
            if not selected_roles:
                errors['roles'] = 'Seleccioná al menos un rol.'
        else:
            selected_roles = usuario.roles

        if not errors:
            usuario.nombre = nombre
            usuario.apellido = apellido
            if is_admin:
                usuario.email = email
                usuario.roles = selected_roles

            db.session.commit()
            flash('Usuario actualizado correctamente.', 'success')
            return redirect(url_for('main.dashboard'))

    roles_actuales = [rol.nombre for rol in usuario.roles] or ['Usuario común']
    form_data = {
        'nombre_apellido': request.form.get(
            'nombre_apellido',
            f'{usuario.nombre} {usuario.apellido}',
        ),
        'email': request.form.get('email', usuario.email or ''),
        'roles': request.form.getlist('roles') if request.method == 'POST' else roles_actuales,
    }

    return render_template(
        'editar_usuario.html',
        usuario=usuario,
        roles_disponibles=roles_disponibles,
        is_admin=is_admin,
        form_data=form_data,
        errors=errors,
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


@bp.route('/api/esp32/heartbeat', methods=['POST'])
def api_esp32_heartbeat():
    data = request.get_json(silent=True) or {}
    dispositivo, error = _get_signed_device(data, include_mac=True)

    if not dispositivo:
        return jsonify({'status': 'error', 'message': error}), 403

    dispositivo.mac = str(data.get('mac', '')).strip().upper()
    dispositivo.ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
    dispositivo.firmware_version = str(data.get('firmware_version', '')).strip()[:32] or None
    dispositivo.last_nonce = str(data.get('nonce', '')).strip()
    dispositivo.last_seen = datetime.utcnow()

    db.session.add(DispositivoEvento(
        dispositivo_id=dispositivo.id,
        event_type='heartbeat',
        value=dispositivo.ip_address,
    ))
    db.session.commit()

    return jsonify({'status': 'ok', 'linked': bool(dispositivo.usuario_id)})


@bp.route('/api/esp32/commands', methods=['POST'])
def api_esp32_commands():
    data = request.get_json(silent=True) or {}
    dispositivo, error = _get_signed_device(data, include_mac=False)

    if not dispositivo:
        return jsonify({'status': 'error', 'message': error}), 403

    dispositivo.last_nonce = str(data.get('nonce', '')).strip()
    dispositivo.last_seen = datetime.utcnow()

    comandos = (
        DispositivoComando.query
        .filter_by(dispositivo_id=dispositivo.id, consumed=False)
        .order_by(DispositivoComando.created_at.asc())
        .limit(5)
        .all()
    )
    payload = [cmd.command for cmd in comandos]
    for cmd in comandos:
        cmd.consumed = True
        cmd.consumed_at = datetime.utcnow()

    db.session.commit()
    return jsonify({'status': 'ok', 'commands': payload})


@bp.route('/api/devices/<device_id>/status')
@login_required
def api_device_status(device_id):
    dispositivo = Dispositivo.query.filter_by(
        device_id=_clean_device_id(device_id),
        usuario_id=current_user.id,
    ).first_or_404()

    return jsonify({
        'device_id': dispositivo.device_id,
        'mac': dispositivo.mac,
        'ip_address': dispositivo.ip_address,
        'firmware_version': dispositivo.firmware_version,
        'last_seen': dispositivo.last_seen.isoformat() if dispositivo.last_seen else None,
    })


@bp.route('/api/devices/<device_id>/command', methods=['POST'])
@login_required
def api_device_command(device_id):
    data = request.get_json(silent=True) or request.form
    command = str(data.get('command', '')).strip()

    if command not in ALLOWED_DEVICE_COMMANDS:
        return jsonify({'status': 'error', 'message': 'invalid_command'}), 400

    dispositivo = Dispositivo.query.filter_by(
        device_id=_clean_device_id(device_id),
        usuario_id=current_user.id,
    ).first_or_404()

    db.session.add(DispositivoComando(
        dispositivo_id=dispositivo.id,
        command=command,
    ))
    db.session.commit()

    return jsonify({'status': 'ok'})


@bp.route('/api/deteccion', methods=['POST'])
def api_deteccion():
    """
    Endpoint interno para registrar detecciones desde el script de visión artificial.
    """
    data = request.get_json(silent=True)

    if not data or data.get('api_key') != current_app.config['VISION_API_KEY']:
        abort(401)

    try:
        device_id = _clean_device_id(data.get('device_id'))
        dispositivo = None
        if device_id:
            dispositivo = Dispositivo.query.filter_by(device_id=device_id).first()
            if not dispositivo or dispositivo.usuario_id != int(data['usuario_id']):
                return {'status': 'error', 'message': 'device_not_owned_by_user'}, 403

        guardar_deteccion_offline(
            usuario_id=data['usuario_id'],
            tipo_evento=data['tipo_evento'],
            video_path=data['video_path'],
            valor_ear=data.get('valor_ear'),
            valor_pitch=data.get('valor_pitch'),
            duracion_alerta=data.get('duracion_alerta'),
        )
        if dispositivo:
            db.session.add(DispositivoEvento(
                dispositivo_id=dispositivo.id,
                event_type='deteccion',
                value=data['tipo_evento'],
            ))
            db.session.commit()
        return {'status': 'ok', 'message': 'Detección guardada localmente.'}, 201
    except Exception as e:
        logger.error(f'[API] Error al guardar detección: {e}')
        return {'status': 'error', 'message': str(e)}, 500


def _reset_token(email):
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset')


def _verify_token(token, max_age=3600):
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        return serializer.loads(token, salt='password-reset', max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None


@bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = Usuario.query.filter_by(email=email).first()

        if user:
            token = _reset_token(email)
            link = url_for('main.reset_password', token=token, _external=True)
            msg = Message('Recuperación de contraseña — Vision', recipients=[email])
            msg.body = f'Usá este link para restablecer tu contraseña (válido 1 hora):\n{link}'
            try:
                mail.send(msg)
            except Exception as e:
                logger.error(f'[MAIL] Error al enviar email: {e}')

        flash('Si el correo existe, recibiras un link en breve.', 'info')
        return redirect(url_for('main.login'))

    return render_template('forgot_password.html')


@bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = _verify_token(token)
    if not email:
        flash('El link es invalido o expiro.', 'danger')
        return redirect(url_for('main.forgot_password'))

    if request.method == 'POST':
        pw = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        ok, msg = valid_password(pw)

        if not ok:
            flash(msg, 'danger')
        elif pw != pw2:
            flash('Las contraseñas no coinciden.', 'danger')
        else:
            user = Usuario.query.filter_by(email=email).first()
            if user:
                user.set_password(pw)
                db.session.commit()
                flash('Contraseña actualizada. Podes iniciar sesion.', 'success')
                return redirect(url_for('main.login'))

    return render_template('reset_password.html', token=token)
