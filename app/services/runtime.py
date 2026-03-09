from dataclasses import dataclass

from app.config import Settings
from app.services.llm_service import LLMService
from app.services.retriever import Retriever
from app.services.session_token import SessionTokenManager
from app.services.stt_service import STTService
from app.services.tts_service import TTSService


@dataclass
class AppServices:
    settings: Settings
    retriever: Retriever
    llm: LLMService
    stt: STTService
    tts: TTSService
    session_tokens: SessionTokenManager
