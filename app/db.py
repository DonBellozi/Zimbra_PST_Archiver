from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import env

class Base(DeclarativeBase): pass

engine = create_engine(env.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def db_session():
    with SessionLocal() as db:
        yield db

