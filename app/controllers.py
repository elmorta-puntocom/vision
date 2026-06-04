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
from .models import Deteccion, EstadisticaSeguridad, Rol, Usuario
from .services import (
    contar_pendientes_usuario,
    guardar_deteccion_offline,
    mysql_disponible,
)


bp = Blueprint('main', __name__)

DEFAULT_ROLES = ('Administrador', 'Usuario común')


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


def _init_stats(usuario_id):
    if not EstadisticaSeguridad.query.filter_by(usuario_id=usuario_id).first():
        db.session.add(EstadisticaSeguridad(usuario_id=usuario_id))
        db.session.commit()


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

            u = Usuario(nombre=nombre, apellido=apellido, email=email)
            u.set_password(pw)
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
    if user_id != current_user.id and not current_user.has_role('Administrador'):
        abort(403)

    _ensure_default_roles()
    usuario = Usuario.query.get_or_404(user_id)
    roles_disponibles = Rol.query.order_by(Rol.nombre.asc()).all()
    errors = {}

    if request.method == 'POST':
        nombre_apellido = request.form.get('nombre_apellido', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        roles_seleccionados = request.form.getlist('roles')
        nombre, apellido = split_full_name(nombre_apellido)

        if not nombre_apellido:
            errors['nombre_apellido'] = 'El nombre y apellido son obligatorios.'
        elif not nombre or not apellido:
            errors['nombre_apellido'] = 'Ingresá nombre y apellido.'

        if email and not valid_email(email):
            errors['email'] = 'Ingresá un e-mail válido.'
        elif email:
            existing_email = Usuario.query.filter_by(email=email).first()
            if existing_email and existing_email.id != usuario.id:
                errors['email'] = 'Ese e-mail ya está registrado.'

        if not valid_optional_password(password):
            errors['password'] = 'La contraseña debe tener mínimo 6 caracteres.'

        selected_roles = Rol.query.filter(Rol.nombre.in_(roles_seleccionados)).all()
        if not selected_roles:
            errors['roles'] = 'Seleccioná al menos un rol.'

        if not errors:
            usuario.nombre = nombre
            usuario.apellido = apellido
            if email:
                usuario.email = email
            if password:
                usuario.set_password(password)
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
