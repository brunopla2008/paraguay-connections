import hashlib, hmac, base64, json, time
from functools import wraps
from flask import request, jsonify
from db import get_db

SECRET = "loteadmin_secret_2025_paraguay"

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64d(s):
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + '=' * (pad % 4))

def crear_token(usuario_id, rol, nombre):
    header = _b64(json.dumps({'alg':'HS256','typ':'JWT'}).encode())
    payload = _b64(json.dumps({
        'sub': usuario_id,
        'rol': rol,
        'nombre': nombre,
        'exp': int(time.time()) + 60*60*24*7  # 7 días
    }).encode())
    firma = _b64(hmac.new(
        SECRET.encode(),
        f"{header}.{payload}".encode(),
        hashlib.sha256
    ).digest())
    return f"{header}.{payload}.{firma}"

def verificar_token(token):
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        header, payload, firma = parts
        firma_esperada = _b64(hmac.new(
            SECRET.encode(),
            f"{header}.{payload}".encode(),
            hashlib.sha256
        ).digest())
        if not hmac.compare_digest(firma, firma_esperada):
            return None
        data = json.loads(_b64d(payload))
        if data.get('exp', 0) < int(time.time()):
            return None
        return data
    except Exception:
        return None

def requiere_auth(roles=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                return jsonify({'error': 'Token requerido'}), 401
            token_data = verificar_token(auth[7:])
            if not token_data:
                return jsonify({'error': 'Token inválido o expirado'}), 401
            if roles and token_data.get('rol') not in roles:
                return jsonify({'error': 'Sin permisos para esta acción'}), 403
            request.usuario = token_data
            return f(*args, **kwargs)
        return wrapper
    return decorator
