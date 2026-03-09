from typing import List, Optional

from pydantic import BaseModel, Field


class MovieObject(BaseModel):
    title: str
    overview: str
    genres: List[str] = Field(default_factory=list)
    top_actors: List[str] = Field(default_factory=list)
    director: str = ""
    poster_url: str = ""


class RecommendRequest(BaseModel):
    query: str
    include_audio: bool = False


class RecommendResponse(BaseModel):
    text: str
    movies: List[MovieObject] = Field(default_factory=list)
    audio_url: Optional[str] = None


class VoiceChatResponse(BaseModel):
    session_id: str
    session_token: str = ""
    user_text: Optional[str] = None
    text: str
    audio_url: Optional[str] = None
    movies: List[MovieObject] = Field(default_factory=list)
    end_session: bool = False


class StartVoiceSessionResponse(BaseModel):
    session_id: str
    session_token: str = ""
    text: str
    audio_url: Optional[str] = None
