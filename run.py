import os

from app import create_app, db, logger
from app.services import start_sync_thread


app = create_app()


def inicializar_mysql():
    with app.app_context():
        try:
            db.create_all()
            logger.info('[DB] Tablas MySQL verificadas/creadas.')
        except Exception as e:
            logger.error(f'[DB] No se pudo conectar a MySQL al inicio: {e}')
            logger.warning(
                '[DB] El servidor arrancará de todas formas. '
                'Las detecciones se guardarán offline hasta que MySQL esté disponible.'
            )


if __name__ == '__main__':
    app.debug = True
    inicializar_mysql()

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        start_sync_thread(app)

    app.run(debug=True, host='0.0.0.0', port=5050)
