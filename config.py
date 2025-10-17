from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv() 

class Settings(BaseSettings):
    AIPIPE_KEY: str 
    AIPIPE_URL: str 
    GITHUB_TOKEN: str 

    STUDENT_SECRET: str
    GITHUB_USERNAME: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

def get_settings() -> Settings:
    return Settings()

