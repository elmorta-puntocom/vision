import logging
import os
from pathlib import Path

from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
mail = Mail()

login_manager.login_view = 'main.login'
login_manager.login_message = 'Iniciá sesión para continuar.'
login_manager.login_message_category = 'warning'


def _default_offline_sqlite_uri():
    project_root = Path(__file__).resolve().parent.parent
    legacy_db = project_root / 'vision_app' / 'vision_offline.db'
    offline_db = legacy_db if legacy_db.exists() else project_root / 'vision_offline.db'
    return f"sqlite:///{offline_db.as_posix()}"


def create_app():
    app = Flask(__name__)

    mysql_user = os.environ.get('MYSQL_USER', 'root')
    mysql_password = os.environ.get('MYSQL_PASSWORD', '')
    mysql_host = os.environ.get('MYSQL_HOST', '127.0.0.1')
    mysql_port = os.environ.get('MYSQL_PORT', '3306')
    mysql_db = os.environ.get('MYSQL_DB', 'vision_db')

    mysql_uri = (
        f'mysql+pymysql://{mysql_user}:{mysql_password}'
        f'@{mysql_host}:{mysql_port}/{mysql_db}?charset=utf8mb4'
    )

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'vision-dev-key-change-in-prod')
    app.config['VISION_API_KEY'] = os.environ.get('VISION_API_KEY', 'vision-internal-key')
    app.config['MP_ACCESS_TOKEN'] = os.environ.get('MP_ACCESS_TOKEN', '')
    app.config['MP_PUBLIC_KEY'] = os.environ.get('MP_PUBLIC_KEY', '')
    app.config['SQLALCHEMY_DATABASE_URI'] = mysql_uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'connect_args': {'connect_timeout': 5},
    }
    app.config['OFFLINE_SQLITE_URI'] = os.environ.get(
        'OFFLINE_SQLITE_URI',
        _default_offline_sqlite_uri(),
    )

    # ── Mail ── configurar e inicializar ANTES de registrar el blueprint
    app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS']        = True
    app.config['MAIL_USE_SSL']        = False
    app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME')
    app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)          # ← ahora va junto con las otras extensiones

    from .models import Usuario

    @login_manager.user_loader
    def load_user(uid):
        try:
            return Usuario.query.get(int(uid))
        except Exception:
            return None

    from .controllers import bp as main_bp
    from .services import init_offline_storage

    init_offline_storage(app)
    app.register_blueprint(main_bp)

    return app