from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import env

class Base(DeclarativeBase): pass

connect_args={"check_same_thread":False,"timeout":30} if env.database_url.startswith("sqlite") else {}
engine = create_engine(env.database_url, pool_pre_ping=True, connect_args=connect_args)

if env.database_url.startswith("sqlite"):
    @event.listens_for(engine,"connect")
    def sqlite_pragmas(connection,_):
        cursor=connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def db_session():
    with SessionLocal() as db:
        yield db

def initialize_schema():
    """Serialise additive SQLite DDL when web and worker start together."""
    with engine.connect() as connection:
        if engine.dialect.name == 'sqlite': connection.exec_driver_sql('BEGIN IMMEDIATE')
        Base.metadata.create_all(connection)
        connection.commit()
