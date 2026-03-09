import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.models.schemas import StartVoiceSessionResponse, VoiceChatResponse
from app.services.llm_service import (
    UNRELATED_REDIRECT_EN,
    UNRELATED_REDIRECT_HI,
    check_identity,
    detect_output_language,
    generate_conversation_text,
    generate_grounded_recommendation_text,
    identity_response,
    is_allowed_query,
    is_recommendation_intent,
    is_small_talk_query,
    policy_response_for_query,
)
from app.services.runtime import AppServices

router = APIRouter(tags=["voice"])
REPEAT_PROMPT_TEXT = "I could not clearly hear that. Please repeat your question."
END_SESSION_TEXT = "Okay, no problem. Catch you later."
KILL_SWITCH_PHRASES = [
    "no thank you",
]


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s\u0900-\u097F]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _is_kill_switch_utterance(text: str) -> bool:
    cleaned = _normalize_text(text)
    return any(phrase in cleaned for phrase in KILL_SWITCH_PHRASES)


async def _safe_tts(services: AppServices, text: str) -> str | None:
    try:
        return await services.tts.synthesize(text)
    except HTTPException:
        return None


@router.post("/start-voice-session", response_model=StartVoiceSessionResponse)
async def start_voice_session(request: Request) -> StartVoiceSessionResponse:
    services: AppServices = request.app.state.services
    session_id, session_token, greeting_text = services.session_tokens.start_session()

    # Prefer runtime TTS so greeting speed/voice stays aligned with current settings.
    greeting_url = await _safe_tts(services, greeting_text)
    if not greeting_url:
        fallback_url = services.settings.greeting_audio_url
        greeting_file = services.settings.static_path / fallback_url.replace("/static/", "", 1)
        greeting_url = fallback_url if greeting_file.exists() else None

    return StartVoiceSessionResponse(
        session_id=session_id,
        session_token=session_token,
        text=greeting_text,
        audio_url=greeting_url,
    )


@router.post("/voice-chat", response_model=VoiceChatResponse)
async def voice_chat(
    request: Request,
    session_token: str = Form(...),
    audio: UploadFile = File(...),
    session_id: str = Form(default=""),
) -> VoiceChatResponse:
    services: AppServices = request.app.state.services

    if not audio.filename:
        raise HTTPException(status_code=400, detail="audio file is required")

    state = services.session_tokens.decode(session_token)
    if session_id and session_id != state.session_id:
        raise HTTPException(status_code=401, detail="session mismatch")

    try:
        query_text = await services.stt.transcribe(audio)
    except HTTPException as exc:
        if exc.status_code == 400:
            audio_url = await _safe_tts(services, REPEAT_PROMPT_TEXT)
            return VoiceChatResponse(
                session_id=state.session_id,
                session_token=session_token,
                user_text=None,
                text=REPEAT_PROMPT_TEXT,
                audio_url=audio_url,
                movies=[],
            )
        raise

    if not query_text:
        audio_url = await _safe_tts(services, REPEAT_PROMPT_TEXT)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=session_token,
            user_text=None,
            text=REPEAT_PROMPT_TEXT,
            audio_url=audio_url,
            movies=[],
        )

    if len(query_text) > services.settings.max_query_chars:
        raise HTTPException(
            status_code=400,
            detail=f"Transcribed query too long (max {services.settings.max_query_chars} characters).",
        )
    output_language = detect_output_language(query_text)

    if _is_kill_switch_utterance(query_text):
        audio_url = await _safe_tts(services, END_SESSION_TEXT)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token="",
            user_text=query_text,
            text=END_SESSION_TEXT,
            audio_url=audio_url,
            movies=[],
            end_session=True,
        )

    identity_type = check_identity(query_text)
    if identity_type:
        response_text = identity_response(identity_type) or ""
        _, new_token = services.session_tokens.update_history(session_token, query_text, response_text)
        audio_url = await _safe_tts(services, response_text)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=new_token,
            user_text=query_text,
            text=response_text,
            audio_url=audio_url,
            movies=[],
            end_session=False,
        )

    if not is_allowed_query(query_text):
        refusal = UNRELATED_REDIRECT_HI if output_language == "hi" else UNRELATED_REDIRECT_EN
        _, new_token = services.session_tokens.update_history(session_token, query_text, refusal)
        audio_url = await _safe_tts(services, refusal)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=new_token,
            user_text=query_text,
            text=refusal,
            audio_url=audio_url,
            movies=[],
            end_session=False,
        )

    policy_text = policy_response_for_query(query_text)
    if policy_text is not None:
        _, new_token = services.session_tokens.update_history(session_token, query_text, policy_text)
        audio_url = await _safe_tts(services, policy_text)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=new_token,
            user_text=query_text,
            text=policy_text,
            audio_url=audio_url,
            movies=[],
            end_session=False,
        )

    if is_small_talk_query(query_text):
        text = await generate_conversation_text(
            llm=services.llm,
            query=query_text,
            history=state.history,
            output_language=output_language,
        )
        _, new_token = services.session_tokens.update_history(session_token, query_text, text)
        audio_url = await _safe_tts(services, text)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=new_token,
            user_text=query_text,
            text=text,
            audio_url=audio_url,
            movies=[],
            end_session=False,
        )

    if not is_recommendation_intent(query_text):
        text = await generate_conversation_text(
            llm=services.llm,
            query=query_text,
            history=state.history,
            output_language=output_language,
        )
        _, new_token = services.session_tokens.update_history(session_token, query_text, text)
        audio_url = await _safe_tts(services, text)
        return VoiceChatResponse(
            session_id=state.session_id,
            session_token=new_token,
            user_text=query_text,
            text=text,
            audio_url=audio_url,
            movies=[],
            end_session=False,
        )

    movies = await services.retriever.retrieve(query_text, top_k=services.settings.top_k)
    movie_dicts = [movie.as_dict() for movie in movies]
    text = await generate_grounded_recommendation_text(
        llm=services.llm,
        query=query_text,
        movies=movie_dicts,
        history=state.history,
        output_language=output_language,
    )
    _, new_token = services.session_tokens.update_history(session_token, query_text, text)
    audio_url = await _safe_tts(services, text)

    return VoiceChatResponse(
        session_id=state.session_id,
        session_token=new_token,
        user_text=query_text,
        text=text,
        audio_url=audio_url,
        movies=movie_dicts,
        end_session=False,
    )
