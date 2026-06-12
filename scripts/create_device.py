import argparse
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import bcrypt, create_app, db  # noqa: E402
from app.models import Dispositivo  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description='Crea un ESP32 vendible en Vision.')
    parser.add_argument('--device-id', default=None, help='Ej: ESP32-8F3A91')
    parser.add_argument('--device-secret', default=None, help='Secreto ya grabado en el ESP32')
    parser.add_argument('--activation-code', default=None, help='Codigo impreso/QR')
    return parser


def main():
    args = build_parser().parse_args()
    device_id = (args.device_id or f"ESP32-{secrets.token_hex(3).upper()}").upper()
    device_secret = args.device_secret or secrets.token_urlsafe(32)
    activation_code = args.activation_code or f"{secrets.randbelow(900000) + 100000}"

    app = create_app()
    with app.app_context():
        db.create_all()

        if Dispositivo.query.filter_by(device_id=device_id).first():
            raise SystemExit(f'Ya existe el dispositivo {device_id}')

        dispositivo = Dispositivo(
            device_id=device_id,
            device_secret=device_secret,
            activation_code_hash=bcrypt.generate_password_hash(activation_code).decode('utf-8'),
        )
        db.session.add(dispositivo)
        db.session.commit()

    print('Dispositivo creado')
    print(f'DEVICE_ID={device_id}')
    print(f'DEVICE_SECRET={device_secret}')
    print(f'ACTIVATION_CODE={activation_code}')
    print('Graba DEVICE_ID y DEVICE_SECRET en el ESP32. Entrega ACTIVATION_CODE al usuario.')


if __name__ == '__main__':
    main()
