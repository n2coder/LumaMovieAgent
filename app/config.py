import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Multilingual Voice AI Movie Assistant"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_api_key: str = ""
    allowed_hosts: str = "localhost,127.0.0.1"
    max_query_chars: int = 500
    max_audio_bytes: int = 10485760
    rate_limit_window_sec: int = 60
    rate_limit_recommend_per_window: int = 30
    rate_limit_voice_per_window: int = 20
    rate_limit_session_start_per_window: int = 20
    session_jwt_secret: str = "change-me-dev-secret"
    session_ttl_minutes: int = 60
    session_max_messages: int = 4
    tts_retention_minutes: int = 120
    tts_max_files: int = 300

    use_fine_tuned: bool = False
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_stt_model: str = "gpt-4o-mini-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    openai_tts_speed: float = 1.3
    openai_tts_instructions: str = (
        "Speak in clear Indian English with a warm, natural, conversational tone. "
        "Sound human and emotionally present, and keep phrasing concise."
    )

    fine_tuned_endpoint: str = ""
    fine_tuned_api_key: str = ""
    request_timeout_sec: int = 45

    top_k: int = 5
    use_vector_retriever: bool = True
    enable_webrtc_audio: bool = True
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    vector_metadata_pkl: str = "app/data/movie_metadata.pkl"
    vector_index_bin: str = "app/data/faiss_index.bin"

    movies_csv: str = "app/data/clean_tmdb_with_posters.csv"
    credits_csv: str = "app/data/tmdb_5000_credits.csv"

    static_dir: str = "app/static"
    audio_dir: str = "app/static/audio"
    greeting_audio_url: str = "/static/audio/greeting_prompt.mp3"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def movies_csv_path(self) -> Path:
        return self.project_root / self.movies_csv

    @property
    def credits_csv_path(self) -> Path:
        return self.project_root / self.credits_csv

    @property
    def vector_metadata_path(self) -> Path:
        return self.project_root / self.vector_metadata_pkl

    @property
    def vector_index_path(self) -> Path:
        return self.project_root / self.vector_index_bin

    @property
    def static_path(self) -> Path:
        return self.project_root / self.static_dir

    @property
    def audio_path(self) -> Path:
        return self.project_root / self.audio_dir

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def allowed_hosts_list(self) -> list[str]:
        hosts = [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if render_host and render_host not in hosts:
            hosts.append(render_host)
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
