from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    line_channel_secret: str
    line_channel_access_token: str
    anthropic_api_key: str
    notion_api_key: str
    notion_index_database_id: str
    notion_log_database_id: str
    github_token: str
    github_repo: str
    github_branch: str = "main"

    class Config:
        env_file = ".env"


settings = Settings()
