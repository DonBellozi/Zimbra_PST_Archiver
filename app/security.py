import base64, hashlib
from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext
from .config import env

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def _fernet():
    try: return Fernet(env.app_secret_key.encode())
    except Exception:
        if env.database_url.startswith("sqlite"):
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"development-only-key").digest()))
        raise RuntimeError("APP_SECRET_KEY must be a valid Fernet key")

def encrypt(value: str) -> str: return _fernet().encrypt(value.encode()).decode()
def decrypt(value: str) -> str:
    try: return _fernet().decrypt(value.encode()).decode()
    except InvalidToken: raise RuntimeError("Cannot decrypt a stored secret: APP_SECRET_KEY changed")

