from pydantic_settings import BaseSettings, SettingsConfigDict

class Env(BaseSettings):
    database_url: str = "sqlite:///./archiver.db"
    app_secret_key: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

env = Env()

