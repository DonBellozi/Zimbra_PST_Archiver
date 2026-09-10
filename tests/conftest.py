import os
from cryptography.fernet import Fernet

# Test processes must never connect to a deployed database or use real secrets.
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['APP_SECRET_KEY'] = Fernet.generate_key().decode()
