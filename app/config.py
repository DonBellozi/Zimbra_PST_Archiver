from pydantic_settings import BaseSettings, SettingsConfigDict

class Env(BaseSettings):
    database_url: str = "sqlite:////data/db/archiver.sqlite3"
    app_secret_key: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

env = Env()
