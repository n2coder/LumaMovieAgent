import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import json
import logging
import math
import re
import time
from urllib.parse import urlparse
from uuid import uuid4

_log = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.routes.recommend import router as recommend_router
from app.routes.voice import router as voice_router
from app.services.llm_service import (
    SYSTEM_PROMPT,
    UNRELATED_REDIRECT_EN,
    UNRELATED_REDIRECT_HI,
    build_conversation_messages,
    check_identity,
    detect_output_language,
    generate_conversation_text,
    generate_grounded_recommendation_text,
    get_llm_service,
    identity_response,
    is_allowed_query,
    is_recommendation_intent,
    is_small_talk_query,
    policy_response_for_query,
)
from app.services.deepgram_stt_service import DeepgramSTTService
from app.services.query_preprocessor import QuerySlots, extract_slots
from app.services.redis_session_store import RedisSessionStore
from app.services.retriever import Retriever
from app.services.runtime import AppServices
from app.services.session_token import GREETING_TEXT, SessionTokenManager
from app.services.stt_service import STTService
from app.services.tts_service import TTSService
from app.services.webrtc_bridge import get_webrtc_bridge


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_production:
        weak_secrets = {"change-me-dev-secret", "change-me-please", "dev-secret", ""}
        if settings.session_jwt_secret in weak_secrets or len(settings.session_jwt_secret) < 32:
            raise RuntimeError("SESSION_JWT_SECRET must be a strong secret (>=32 chars) in production.")
    settings.static_path.mkdir(parents=True, exist_ok=True)
    settings.audio_path.mkdir(parents=True, exist_ok=True)

    retriever = Retriever(
        movies_csv_path=str(settings.movies_csv_path),
        credits_csv_path=str(settings.credits_csv_path),
        embedding_model_name=settings.embedding_model_name,
    )
    redis_store = RedisSessionStore(settings)
    await redis_store.ping()  # Sets _enabled; does not raise on failure

    # Deepgram STT — used when stt_provider == "deepgram"
    deepgram_stt = DeepgramSTTService(settings) if settings.stt_provider == "deepgram" else None

    services = AppServices(
        settings=settings,
        retriever=retriever,
        llm=get_llm_service(settings),
        stt=STTService(settings),
        tts=TTSService(settings),
        session_tokens=SessionTokenManager(settings),
        redis_store=redis_store,
        deepgram_stt=deepgram_stt,
    )
    app.state.services = services
    app.state.greeting_audio_task = asyncio.create_task(
        services.tts.ensure_named_audio(GREETING_TEXT, "greeting_prompt.mp3")
    )
    app.state.webrtc_cleanup_task = webrtc_bridge.start_cleanup_task()
    yield
    greeting_task = getattr(app.state, "greeting_audio_task", None)
    if greeting_task:
        greeting_task.cancel()
    cleanup_task = getattr(app.state, "webrtc_cleanup_task", None)
    if cleanup_task:
        cleanup_task.cancel()
    if deepgram_stt:
        await deepgram_stt.close_all()
    await redis_store.close()


settings = get_settings()
webrtc_bridge = get_webrtc_bridge()
WS_KILL_PHRASES = ("no thank you",)
WS_END_SESSION_TEXT = "Okay, no problem. Catch you later."
SENTENCE_BOUNDARY = re.compile(r"[.!?\u0964](?:['\"\)\]]*)\s*")


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s\u0900-\u097F]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _is_kill_phrase(text: str) -> bool:
    cleaned = _normalize_text(text)
    return any(phrase in cleaned for phrase in WS_KILL_PHRASES)


def _split_completed_sentences(buffer: str) -> tuple[list[str], str]:
    output: list[str] = []
    remaining = buffer
    while True:
        match = SENTENCE_BOUNDARY.search(remaining)
        if not match:
            break
        end = match.end()
        sentence = remaining[:end].strip()
        if sentence:
            output.append(sentence)
        remaining = remaining[end:]
    return output, remaining


def _split_text_sentences(text: str) -> list[str]:
    if not (text or "").strip():
        return []
    sentences, remainder = _split_completed_sentences(text.strip())
    if remainder.strip():
        sentences.append(remainder.strip())
    return sentences


def _coalesce_tts_chunks(parts: list[str], min_chars: int = 70, max_chars: int = 220) -> list[str]:
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for part in parts:
        text = (part or "").strip()
        if not text:
            continue

        candidate = f"{current} {text}".strip() if current else text
        if len(candidate) <= max_chars:
            current = candidate
            if len(current) >= min_chars:
                flush()
            continue

        if current:
            flush()

        if len(text) <= max_chars:
            current = text
            if len(current) >= min_chars:
                flush()
            continue

        # Hard split oversized sentence at nearest whitespace.
        remaining = text
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            cut = window.rfind(" ")
            if cut < int(max_chars * 0.6):
                cut = max_chars
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            current = remaining
            if len(current) >= min_chars:
                flush()

    flush()
    return chunks


def _prepare_tts_units(text: str) -> list[str]:
    parts = [part.strip() for part in _split_text_sentences(text) if part.strip()]
    if not parts:
        return []
    if len(parts) == 1:
        return parts
    first = parts[0]
    rest = _coalesce_tts_chunks(parts[1:])
    return [first, *rest]


def _extract_origin_host(origin_value: str) -> str:
    raw = (origin_value or "").strip()
    if not raw:
        return ""
    try:
        return (urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return ""


def _is_same_origin_request(request: Request) -> bool:
    origin_host = _extract_origin_host(request.headers.get("origin", ""))
    if not origin_host:
        return False
    host = (request.headers.get("host", "") or "").split(":", 1)[0].strip().lower()
    return bool(host) and origin_host == host


def _is_same_origin_websocket(websocket: WebSocket) -> bool:
    origin_host = _extract_origin_host(websocket.headers.get("origin", ""))
    if not origin_host:
        return False
    host = (websocket.headers.get("host", "") or "").split(":", 1)[0].strip().lower()
    return bool(host) and origin_host == host


def _force_progressive_chunk(buffer: str, min_chars: int = 55, target_chars: int = 105) -> tuple[str, str]:
    text = (buffer or "").strip()
    if len(text) < min_chars:
        return "", buffer

    limit = min(len(text), target_chars)
    window = text[:limit]
    pivots = [window.rfind(ch) for ch in (".", "!", "?", "\u0964", ",", ";", ":", "\n")]
    cut = max(pivots)
    if cut >= int(min_chars * 0.6):
        flushed = window[: cut + 1].strip()
        remaining = text[cut + 1 :].lstrip()
        return flushed, remaining

    space = window.rfind(" ")
    if space > int(min_chars * 0.75):
        flushed = window[:space].strip()
        remaining = text[space:].lstrip()
        return flushed, remaining

    return "", buffer


def _build_stream_messages(
    history: list[dict],
    query: str,
    movies: list[dict],
    output_language: str | None = None,
    slots: "QuerySlots | None" = None,
    partial_context: str = "",
) -> list[dict]:
    movie_lines = []
    for i, movie in enumerate(movies, start=1):
        title = str(movie.get("title", "")).strip()
        overview = str(movie.get("overview", "")).strip()
        genres = ", ".join(str(g).strip() for g in (movie.get("genres", []) or [])[:3] if str(g).strip())
        if title:
            movie_lines.append(f"{i}. {title} | Genres: {genres or 'N/A'} | Overview: {overview[:280]}")

    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    language_rule = (
        "Respond in Hindi using Devanagari script only. "
        "CRITICAL: even if the user query is written in Roman/Hinglish letters, your ENTIRE response must be in Devanagari. "
        "Wrong example: 'Kya aap Inception dekhna chahenge?' "
        "Right example: 'क्या आप Inception देखना चाहेंगे?'"
        if lang == "hi"
        else "Respond in English."
    )
    grounding = (
        "You must only recommend movies from the provided candidates. "
        "Do not mention any movie outside the list. "
        "Do not bias recommendations by movie language unless the user explicitly asks for a movie language. "
        "Keep it concise and natural for voice."
    )
    slot_hint = ""
    if slots and not slots.is_empty():
        slot_hint = f"[Structured context: {slots.to_context_string()}]\n"
    partial_hint = f"[Early context from partial speech: {partial_context}]\n" if partial_context else ""
    user_prompt = (
        f"{partial_hint}{slot_hint}"
        f"User query: {query}\n\n"
        f"Candidate movies:\n{chr(10).join(movie_lines)}\n\n"
        f"{language_rule}\n"
        "Every Hindi word must be in Devanagari script. Never write Hindi in Roman letters. "
        "Start with one short opening line. "
        "Recommend exactly two movies from the candidates. "
        "Give one short sentence per movie. "
        "End with one short follow-up question. "
        "No markdown. No bullets."
    )

    messages: list[dict] = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{grounding}"}]
    for item in history[-4:]:
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_prompt})
    return messages


async def _send_json_locked(websocket: WebSocket, lock: asyncio.Lock, payload: dict) -> None:
    async with lock:
        await websocket.send_json(payload)


async def _send_audio_payload(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    peer_id: str | None,
    payload: dict,
) -> None:
    if settings.enable_webrtc_audio and peer_id:
        sent = await webrtc_bridge.send_audio_chunk(peer_id, payload)
        if sent:
            return
    await _send_json_locked(websocket, send_lock, payload)


async def _cached_audio_b64(audio_path) -> str | None:
    try:
        if not audio_path.exists():
            return None
        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
        if not audio_bytes:
            return None
        return base64.b64encode(audio_bytes).decode("ascii")
    except Exception:
        return None


async def _tts_sentence_to_b64(
    services: AppServices,
    sentence: str,
    output_language: str | None = None,
) -> str | None:
    text = (sentence or "").strip()
    if not text:
        return None
    client = getattr(services.tts, "client", None)
    if not client:
        return None

    try:
        speed = min(2.0, max(0.8, float(services.settings.openai_tts_speed or 1.0)))
        # Cap Hindi at 1.35× — natural conversational pace with clear pronunciation
        if output_language == "hi":
            speed = min(speed, 1.35)
        request_payload = {
            "model": services.settings.openai_tts_model,
            "voice": services.settings.openai_tts_voice,
            "input": text,
            "speed": speed,
        }
        if output_language == "hi":
            instructions = (
                "आप Luma हैं — एक जोशीली, दोस्ताना मूवी गाइड। "
                "बोलने का अंदाज़ ऐसा हो जैसे किसी दोस्त से बात हो — पढ़ने जैसा नहीं। "
                "फिल्म का नाम थोड़ा धीरे और साफ़ बोलें, फिर आगे की बात तेज़ रखें। "
                "सवाल पूछते वक्त थोड़ी जिज्ञासा हो आवाज़ में।"
            )
        else:
            instructions = (services.settings.openai_tts_instructions or "").strip()
        if instructions:
            request_payload["instructions"] = instructions

        # Use in-memory synthesis: no disk write/read round-trip
        try:
            response = await client.audio.speech.create(**request_payload)
        except TypeError:
            request_payload.pop("instructions", None)
            response = await client.audio.speech.create(**request_payload)
        return base64.b64encode(response.content).decode("ascii")
    except Exception:
        _log.exception("TTS synthesis failed for sentence (%.40s)", text)
        return None


async def _stream_llm_deltas(services: AppServices, messages: list[dict]):
    client = getattr(services.llm, "client", None)
    if client:
        try:
            stream = await client.chat.completions.create(
                model=services.settings.openai_chat_model,
                messages=messages,
                temperature=0.5,
                max_tokens=250,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    yield delta
            return
        except Exception:
            _log.exception("LLM stream failed; falling back to generate_messages")

    fallback = await services.llm.generate_messages(messages)
    if fallback:
        yield fallback



async def _send_text_with_chunked_tts(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    services: AppServices,
    text: str,
    cancel_event: asyncio.Event,
    output_language: str | None = None,
    peer_id: str | None = None,
) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    units = _prepare_tts_units(normalized)
    if not units:
        await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": normalized})
        return normalized

    # Send all text deltas immediately so the user sees text without waiting for TTS
    for idx, sentence in enumerate(units):
        if cancel_event.is_set():
            return normalized
        display_delta = sentence if idx == 0 else f"\n\n{sentence}"
        await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": display_delta})

    if cancel_event.is_set():
        return normalized

    # Launch all TTS requests concurrently — total wait is max(individual), not sum
    tts_tasks = [
        asyncio.create_task(
            _tts_sentence_to_b64(services, sentence, output_language=output_language)
        )
        for sentence in units
    ]

    # Send audio chunks in order as each task completes
    for idx, (sentence, task) in enumerate(zip(units, tts_tasks)):
        if cancel_event.is_set():
            for t in tts_tasks:
                t.cancel()
            break
        audio_b64 = await task
        if audio_b64 and not cancel_event.is_set():
            await _send_audio_payload(
                websocket,
                send_lock,
                peer_id,
                {
                    "type": "audio_chunk",
                    "index": idx,
                    "sentence": sentence,
                    "audio_b64": audio_b64,
                },
            )
    return normalized


async def _process_voice_turn(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    services: AppServices,
    session_token: str,
    query: str,
    cancel_event: asyncio.Event,
    lang_hint: str | None = None,
    peer_id: str | None = None,
) -> str:
    try:
        state = services.session_tokens.decode(session_token)
    except HTTPException as exc:
        await _send_json_locked(websocket, send_lock, {"type": "error", "detail": exc.detail})
        return session_token

    # Load conversation history from Redis (falls back to empty list if Redis unavailable)
    redis: RedisSessionStore | None = getattr(services, "redis_store", None)
    if redis and redis._enabled:
        state.history = await redis.load(state.session_id)

    # Consume any partial transcript context accumulated while user was speaking
    partial_context = ""
    if redis and redis._enabled:
        partial_context = await redis.get_partial(state.session_id)
        if partial_context:
            await redis.clear_partial(state.session_id)

    user_query = (query or "").strip()
    user_lang = detect_output_language(user_query, lang_hint=lang_hint)
    slots = extract_slots(user_query)
    if not user_query:
        await _send_json_locked(websocket, send_lock, {"type": "error", "detail": "query is required"})
        return session_token

    if len(user_query) > services.settings.max_query_chars:
        await _send_json_locked(
            websocket,
            send_lock,
            {"type": "error", "detail": f"query too long (max {services.settings.max_query_chars} characters)"},
        )
        return session_token

    await _send_json_locked(websocket, send_lock, {"type": "turn_started", "source": "query", "query": user_query})

    movies: list[dict] = []
    full_text = ""
    end_session = False
    should_stream_llm = False
    stream_messages: list[dict] | None = None

    if _is_kill_phrase(user_query):
        end_session = True
        full_text = WS_END_SESSION_TEXT
    else:
        identity_type = check_identity(user_query)
        if identity_type:
            full_text = identity_response(identity_type) or ""
        elif not is_allowed_query(user_query):
            full_text = UNRELATED_REDIRECT_HI if user_lang == "hi" else UNRELATED_REDIRECT_EN
        else:
            policy_text = policy_response_for_query(user_query)
            if policy_text is not None:
                if policy_text in {UNRELATED_REDIRECT_EN, UNRELATED_REDIRECT_HI}:
                    full_text = UNRELATED_REDIRECT_HI if user_lang == "hi" else UNRELATED_REDIRECT_EN
                else:
                    full_text = policy_text
            elif is_small_talk_query(user_query):
                should_stream_llm = True
                stream_messages = build_conversation_messages(
                    query=user_query,
                    history=state.history,
                    output_language=user_lang,
                    slots=slots,
                )
            elif not is_recommendation_intent(user_query):
                should_stream_llm = True
                stream_messages = build_conversation_messages(
                    query=user_query,
                    history=state.history,
                    output_language=user_lang,
                    slots=slots,
                )
            else:
                retrieved = await services.retriever.retrieve(user_query, top_k=services.settings.top_k)
                movies = [m.as_dict() for m in retrieved]
                if movies:
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {
                            "type": "movies_update",
                            "movies": movies,
                        },
                    )
                should_stream_llm = bool(movies)
                if should_stream_llm:
                    stream_messages = _build_stream_messages(
                        state.history,
                        user_query,
                        movies,
                        output_language=user_lang,
                        slots=slots,
                        partial_context=partial_context,
                    )
                else:
                    full_text = await generate_grounded_recommendation_text(
                        llm=services.llm,
                        query=user_query,
                        movies=movies,
                        history=state.history,
                        output_language=user_lang,
                    )

    if should_stream_llm:
        messages = stream_messages or build_conversation_messages(
            query=user_query,
            history=state.history,
            output_language=user_lang,
        )
        # tts_futures: ordered list of (sentence_text, asyncio.Task[audio_b64|None]).
        # Tasks are spawned as each sentence arrives from the LLM stream.
        # A concurrent drain task sends audio in order as each TTS task completes,
        # so the user hears the first sentence while the LLM is still generating the rest.
        tts_futures: list[tuple[str, "asyncio.Task[str | None]"]] = []
        llm_done = asyncio.Event()
        buffer = ""
        streamed_text = ""
        tts_parts: list[str] = []
        tts_chars = 0
        first_dispatched = False
        last_emit_at = time.monotonic()

        def _dispatch_tts_unit(unit: str) -> None:
            nonlocal first_dispatched
            text = unit.strip()
            if not text:
                return
            task: asyncio.Task[str | None] = asyncio.create_task(
                _tts_sentence_to_b64(services, text, output_language=user_lang)
            )
            tts_futures.append((text, task))
            first_dispatched = True

        async def flush_tts_parts(force: bool = False) -> None:
            nonlocal tts_parts, tts_chars
            if not tts_parts:
                return
            # Fast-path: dispatch first unit immediately regardless of size
            if not first_dispatched:
                first_unit = tts_parts.pop(0).strip()
                tts_chars = sum(len(p) for p in tts_parts)
                if first_unit:
                    _dispatch_tts_unit(first_unit)
                if not tts_parts:
                    return
            if not force and tts_chars < 60:
                return
            for unit in _coalesce_tts_chunks(tts_parts):
                _dispatch_tts_unit(unit)
            tts_parts = []
            tts_chars = 0

        async def _drain_tts() -> None:
            """Send audio chunks in order as TTS tasks complete — runs concurrently with LLM stream."""
            audio_index = 0
            sent_up_to = 0
            while True:
                if cancel_event.is_set():
                    break
                if sent_up_to < len(tts_futures):
                    sentence, task = tts_futures[sent_up_to]
                    try:
                        audio_b64 = await task
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        audio_b64 = None
                    sent_up_to += 1
                    if audio_b64 and not cancel_event.is_set():
                        await _send_audio_payload(
                            websocket,
                            send_lock,
                            peer_id,
                            {
                                "type": "audio_chunk",
                                "index": audio_index,
                                "sentence": sentence,
                                "audio_b64": audio_b64,
                            },
                        )
                        audio_index += 1
                elif llm_done.is_set():
                    break
                else:
                    await asyncio.sleep(0.02)  # wait for more TTS tasks to be dispatched

        drain_task = asyncio.create_task(_drain_tts())

        try:
            async for delta in _stream_llm_deltas(services, messages):
                if cancel_event.is_set():
                    break
                streamed_text += delta
                await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": delta})
                buffer += delta
                ready, buffer = _split_completed_sentences(buffer)
                for sentence in ready:
                    tts_parts.append(sentence)
                    tts_chars += len(sentence)
                if ready:
                    await flush_tts_parts(force=False)
                    last_emit_at = time.monotonic()
                if not ready:
                    now = time.monotonic()
                    if len(buffer.strip()) >= 55 and now - last_emit_at >= 0.22:
                        early, buffer = _force_progressive_chunk(buffer)
                        if early:
                            tts_parts.append(early)
                            tts_chars += len(early)
                            await flush_tts_parts(force=False)
                            last_emit_at = now

            if not cancel_event.is_set() and buffer.strip():
                tts_parts.append(buffer.strip())
                tts_chars += len(buffer.strip())
            await flush_tts_parts(force=True)
        except Exception:
            _log.exception("LLM streaming loop failed")
        finally:
            llm_done.set()
            if cancel_event.is_set():
                drain_task.cancel()
                for _, t in tts_futures:
                    t.cancel()

        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        full_text = streamed_text.strip()
        if not full_text and not cancel_event.is_set():
            full_text = await generate_grounded_recommendation_text(
                llm=services.llm,
                query=user_query,
                movies=movies,
                history=state.history,
                output_language=user_lang,
            )
            await _send_text_with_chunked_tts(
                websocket,
                send_lock,
                services,
                full_text,
                cancel_event,
                output_language=user_lang,
                peer_id=peer_id,
            )
    else:
        full_text = await _send_text_with_chunked_tts(
            websocket,
            send_lock,
            services,
            full_text,
            cancel_event,
            output_language=user_lang,
            peer_id=peer_id,
        )

    if cancel_event.is_set():
        await _send_json_locked(websocket, send_lock, {"type": "turn_cancelled"})
        return session_token

    if end_session:
        await _send_json_locked(
            websocket,
            send_lock,
            {
                "type": "turn_complete",
                "session_id": state.session_id,
                "session_token": "",
                "full_text": full_text,
                "movies": [],
                "end_session": True,
            },
        )
        return ""

    # Persist history to Redis; re-encode a thin JWT to slide the expiry window
    updated_history = list(state.history)
    if user_query:
        updated_history.append({"role": "user", "content": user_query})
    if full_text:
        updated_history.append({"role": "assistant", "content": full_text})
    updated_history = updated_history[-services.settings.session_max_messages:]
    if redis and redis._enabled:
        await redis.save(state.session_id, updated_history, services.settings.session_ttl_minutes)
    new_token = services.session_tokens.encode(state.session_id)
    await _send_json_locked(
        websocket,
        send_lock,
        {
            "type": "turn_complete",
            "session_id": state.session_id,
            "session_token": new_token,
            "full_text": full_text,
            "movies": movies,
            "end_session": False,
        },
    )
    return new_token


def _audio_extension_from_mime(mime_type: str) -> str:
    normalized = (mime_type or "").strip().lower()
    if "webm" in normalized:
        return ".webm"
    if "ogg" in normalized:
        return ".ogg"
    if "mp4" in normalized or "m4a" in normalized:
        return ".m4a"
    if "wav" in normalized:
        return ".wav"
    if "mpeg" in normalized or "mp3" in normalized:
        return ".mp3"
    return ".webm"


def _safe_decode_audio_b64(raw: str) -> bytes:
    payload = (raw or "").strip()
    if not payload:
        return b""
    try:
        return base64.b64decode(payload, validate=True)
    except Exception:
        return b""


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.mount("/static", StaticFiles(directory=str(settings.static_path)), name="static")
if settings.is_production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list or ["*"])

_rate_buckets: dict[str, deque[float]] = {}
_rate_lock = asyncio.Lock()
_ws_rate_buckets: dict[str, deque[float]] = {}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "microphone=(self), geolocation=(), camera=()"
    # Allow ws: only in dev so browsers can connect to localhost; production requires wss:
    ws_origins = "wss:" if settings.is_production else "ws: wss:"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; media-src 'self' https: blob: data:; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' https: data:; "
        f"connect-src 'self' {ws_origins}; frame-ancestors 'none'"
    )
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    return response


@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    protected_paths = {"/recommend", "/voice-chat", "/start-voice-session", "/webrtc/offer"}
    is_webrtc_close_path = path.startswith("/webrtc/close/")
    if settings.app_api_key and (path in protected_paths or is_webrtc_close_path):
        provided = request.headers.get("X-API-Key", "").strip()
        same_origin = _is_same_origin_request(request)
        if not same_origin and provided != settings.app_api_key:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    limit_map = {
        ("POST", "/recommend"): settings.rate_limit_recommend_per_window,
        ("POST", "/voice-chat"): settings.rate_limit_voice_per_window,
        ("POST", "/start-voice-session"): settings.rate_limit_session_start_per_window,
    }
    limit = limit_map.get((method, path))
    if limit:
        now = time.time()
        ip = request.client.host if request.client else "unknown"
        key = f"{ip}:{method}:{path}"
        async with _rate_lock:
            bucket = _rate_buckets.get(key)
            if bucket is None:
                bucket = deque()
                _rate_buckets[key] = bucket
            while bucket and now - bucket[0] >= settings.rate_limit_window_sec:
                bucket.popleft()
            if not bucket:
                _rate_buckets.pop(key, None)
            elif len(bucket) >= limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please retry shortly."},
                )
            else:
                bucket.append(now)

    return await call_next(request)

app.include_router(recommend_router)
app.include_router(voice_router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(settings.static_path / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


class WebRTCOfferRequest(BaseModel):
    sdp: str
    type: str


@app.post("/webrtc/offer")
async def webrtc_offer(payload: WebRTCOfferRequest) -> dict:
    if not settings.enable_webrtc_audio:
        raise HTTPException(status_code=503, detail="WebRTC audio is disabled.")
    return await webrtc_bridge.create_answer(sdp=payload.sdp, type_=payload.type)


@app.post("/webrtc/close/{peer_id}")
async def webrtc_close(peer_id: str) -> dict:
    await webrtc_bridge.close_peer(peer_id)
    return {"status": "closed"}


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    if settings.app_api_key:
        # Accept the key via header only — query params appear in server logs and browser history.
        provided = websocket.headers.get("x-api-key", "").strip()
        same_origin = _is_same_origin_websocket(websocket)
        if not same_origin and provided != settings.app_api_key:
            await websocket.close(code=4401)
            return

    await websocket.accept()
    services: AppServices = websocket.app.state.services
    send_lock = asyncio.Lock()
    client_ip = websocket.client.host if websocket.client else "unknown"

    current_session_token = ""
    current_peer_id = ""
    active_turn_task: asyncio.Task | None = None
    active_cancel_event = asyncio.Event()
    ws_payload_limit_bytes = max(24_000, int(math.ceil(services.settings.max_audio_bytes * 1.6)))

    async def cancel_active_turn() -> None:
        nonlocal active_turn_task, active_cancel_event
        if active_turn_task and not active_turn_task.done():
            active_cancel_event.set()
            active_turn_task.cancel()
            try:
                await active_turn_task
            except BaseException:
                pass
        active_turn_task = None
        active_cancel_event = asyncio.Event()

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > ws_payload_limit_bytes:
                await _send_json_locked(websocket, send_lock, {"type": "error", "detail": "payload too large"})
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await _send_json_locked(websocket, send_lock, {"type": "error", "detail": "invalid JSON payload"})
                continue

            msg_type = str(payload.get("type", "")).strip()

            if msg_type == "start_session":
                candidate_token = str(payload.get("session_token", "")).strip()
                peer_from_client = str(payload.get("peer_id", "")).strip()
                if peer_from_client:
                    current_peer_id = peer_from_client
                silent_resume = payload.get("silent", False) is True or str(payload.get("silent", "")).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if candidate_token:
                    try:
                        state = services.session_tokens.decode(candidate_token)
                        current_session_token = candidate_token
                        greeting_text = state.history[-1]["content"] if state.history else "Hi! I'm Luma."
                        session_id = state.session_id
                    except HTTPException:
                        session_id, current_session_token, greeting_text = services.session_tokens.start_session()
                else:
                    session_id, current_session_token, greeting_text = services.session_tokens.start_session()

                await _send_json_locked(
                    websocket,
                    send_lock,
                    {
                        "type": "session_started",
                        "session_id": session_id,
                        "session_token": current_session_token,
                        "text": greeting_text,
                    },
                )

                if silent_resume and candidate_token:
                    continue

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = current_session_token
                # Snapshot peer_id by value so the closure isn't affected by later mutations
                peer_id_snapshot = current_peer_id

                async def run_greeting_turn(
                    _pid: str = peer_id_snapshot,
                    _cancel: asyncio.Event = active_cancel_event,
                ) -> None:
                    await _send_json_locked(websocket, send_lock, {"type": "turn_started", "source": "greeting"})
                    spoken_text = greeting_text.strip()
                    greeting_path = services.settings.audio_path / "greeting_prompt.mp3"
                    greeting_audio_b64 = await _cached_audio_b64(greeting_path)
                    if greeting_audio_b64:
                        await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": spoken_text})
                        await _send_audio_payload(
                            websocket,
                            send_lock,
                            _pid,
                            {
                                "type": "audio_chunk",
                                "index": 0,
                                "sentence": spoken_text,
                                "audio_b64": greeting_audio_b64,
                            },
                        )
                    else:
                        spoken_text = await _send_text_with_chunked_tts(
                            websocket=websocket,
                            send_lock=send_lock,
                            services=services,
                            text=greeting_text,
                            cancel_event=_cancel,
                            output_language="en",
                            peer_id=_pid,
                        )
                    if _cancel.is_set():
                        await _send_json_locked(websocket, send_lock, {"type": "turn_cancelled"})
                        return
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {
                            "type": "turn_complete",
                            "session_id": session_id,
                            "session_token": token_snapshot,
                            "full_text": spoken_text,
                            "movies": [],
                            "end_session": False,
                        },
                    )

                active_turn_task = asyncio.create_task(run_greeting_turn())

            elif msg_type == "user_query":
                query = str(payload.get("query", "")).strip()
                lang_hint = str(payload.get("lang_hint", "")).strip().lower()
                peer_from_client = str(payload.get("peer_id", "")).strip()
                if peer_from_client:
                    current_peer_id = peer_from_client
                token_from_client = str(payload.get("session_token", "")).strip()
                if token_from_client:
                    current_session_token = token_from_client
                if not current_session_token:
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {"type": "error", "detail": "session is not initialized"},
                    )
                    continue
                if not query:
                    continue
                now = time.time()
                ws_query_key = f"{client_ip}:ws:user_query"
                async with _rate_lock:
                    ws_bucket = _ws_rate_buckets.get(ws_query_key)
                    if ws_bucket is None:
                        ws_bucket = deque()
                        _ws_rate_buckets[ws_query_key] = ws_bucket
                    while ws_bucket and now - ws_bucket[0] >= services.settings.rate_limit_window_sec:
                        ws_bucket.popleft()
                    if not ws_bucket:
                        _ws_rate_buckets.pop(ws_query_key, None)
                    elif len(ws_bucket) >= services.settings.rate_limit_voice_per_window:
                        await _send_json_locked(
                            websocket,
                            send_lock,
                            {"type": "error", "detail": "Rate limit exceeded. Please retry shortly."},
                        )
                        continue
                    else:
                        ws_bucket.append(now)

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = current_session_token
                peer_id_snapshot = current_peer_id

                async def run_turn(
                    _pid: str = peer_id_snapshot,
                    _cancel: asyncio.Event = active_cancel_event,
                ) -> None:
                    nonlocal current_session_token
                    new_token = await _process_voice_turn(
                        websocket=websocket,
                        send_lock=send_lock,
                        services=services,
                        session_token=token_snapshot,
                        query=query,
                        cancel_event=_cancel,
                        lang_hint=lang_hint,
                        peer_id=_pid,
                    )
                    current_session_token = new_token

                active_turn_task = asyncio.create_task(run_turn())

            elif msg_type == "user_audio":
                token_from_client = str(payload.get("session_token", "")).strip()
                if token_from_client:
                    current_session_token = token_from_client
                if not current_session_token:
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {"type": "error", "detail": "session is not initialized"},
                    )
                    continue
                peer_from_client = str(payload.get("peer_id", "")).strip()
                if peer_from_client:
                    current_peer_id = peer_from_client

                audio_b64 = str(payload.get("audio_b64", "")).strip()
                mime_type = str(payload.get("mime_type", "")).strip()
                lang_hint = str(payload.get("lang_hint", "")).strip().lower()
                audio_bytes = _safe_decode_audio_b64(audio_b64)
                if not audio_bytes:
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {"type": "error", "detail": "invalid or empty audio payload"},
                    )
                    continue
                if len(audio_bytes) > services.settings.max_audio_bytes:
                    await _send_json_locked(
                        websocket,
                        send_lock,
                        {"type": "error", "detail": f"audio too large (max {services.settings.max_audio_bytes} bytes)"},
                    )
                    continue

                now = time.time()
                ws_audio_key = f"{client_ip}:ws:user_audio"
                async with _rate_lock:
                    ws_bucket = _ws_rate_buckets.get(ws_audio_key)
                    if ws_bucket is None:
                        ws_bucket = deque()
                        _ws_rate_buckets[ws_audio_key] = ws_bucket
                    while ws_bucket and now - ws_bucket[0] >= services.settings.rate_limit_window_sec:
                        ws_bucket.popleft()
                    if not ws_bucket:
                        _ws_rate_buckets.pop(ws_audio_key, None)
                    elif len(ws_bucket) >= services.settings.rate_limit_voice_per_window:
                        await _send_json_locked(
                            websocket,
                            send_lock,
                            {"type": "error", "detail": "Rate limit exceeded. Please retry shortly."},
                        )
                        continue
                    else:
                        ws_bucket.append(now)

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = current_session_token
                peer_id_snapshot = current_peer_id
                ext = _audio_extension_from_mime(mime_type)

                async def run_audio_turn(
                    _pid: str = peer_id_snapshot,
                    _cancel: asyncio.Event = active_cancel_event,
                ) -> None:
                    nonlocal current_session_token
                    # Try Deepgram stream first (already accumulating since first chunk)
                    query = ""
                    _dg: DeepgramSTTService | None = getattr(services, "deepgram_stt", None)
                    if _dg and _dg._client:
                        try:
                            _sid = services.session_tokens.decode(token_snapshot).session_id
                        except Exception:
                            _sid = ""
                        if _sid and _dg.has_open_stream(_sid):
                            # Send the final full blob too so nothing is missed
                            await _dg.send_chunk(_sid, audio_bytes)
                            query = await _dg.close_stream(_sid)

                    # Fallback: send full blob to Whisper if Deepgram gave nothing
                    if not query:
                        try:
                            query = await services.stt.transcribe_bytes(
                                audio_bytes, filename=f"ws_input{ext}", lang_hint=lang_hint
                            )
                        except HTTPException as exc:
                            await _send_json_locked(
                                websocket,
                                send_lock,
                                {"type": "error", "detail": exc.detail},
                            )
                            return

                    query = (query or "").strip()
                    if not query:
                        await _send_json_locked(
                            websocket,
                            send_lock,
                            {"type": "error", "detail": "could not transcribe audio"},
                        )
                        return
                    new_token = await _process_voice_turn(
                        websocket=websocket,
                        send_lock=send_lock,
                        services=services,
                        session_token=token_snapshot,
                        query=query,
                        cancel_event=_cancel,
                        lang_hint=lang_hint,
                        peer_id=_pid,
                    )
                    current_session_token = new_token

                active_turn_task = asyncio.create_task(run_audio_turn())

            elif msg_type == "utterance_start":
                # WebRTC uplink: signal server to begin buffering PCM from the audio track
                if services.settings.enable_webrtc_uplink and current_peer_id:
                    await webrtc_bridge.start_utterance(current_peer_id)

            elif msg_type == "utterance_end":
                # WebRTC uplink: user stopped speaking — pull buffered PCM, convert to WAV, run STT
                if not services.settings.enable_webrtc_uplink:
                    continue
                uplink_peer_id = str(payload.get("peer_id", current_peer_id)).strip()
                lang_hint = str(payload.get("lang_hint", "en")).strip()
                token_up = str(payload.get("session_token", current_session_token)).strip()

                result = await webrtc_bridge.end_utterance(uplink_peer_id)
                if not result:
                    await _send_json_locked(
                        websocket, send_lock,
                        {"type": "error", "detail": "no audio buffered from WebRTC uplink"},
                    )
                    continue

                wav_bytes, _sr, _ch = result
                if not wav_bytes or len(wav_bytes) < 200:
                    await _send_json_locked(
                        websocket, send_lock,
                        {"type": "error", "detail": "WebRTC audio too short"},
                    )
                    continue

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = token_up or current_session_token
                peer_id_snapshot = uplink_peer_id or current_peer_id

                async def run_webrtc_audio_turn(
                    _pid: str = peer_id_snapshot,
                    _cancel: asyncio.Event = active_cancel_event,
                    _wav: bytes = wav_bytes,
                    _lang: str = lang_hint,
                    _tok: str = token_snapshot,
                ) -> None:
                    nonlocal current_session_token
                    # Try Deepgram stream first (chunks already streamed via audio_chunk_partial)
                    query = ""
                    _dg: DeepgramSTTService | None = getattr(services, "deepgram_stt", None)
                    if _dg and _dg._client:
                        try:
                            _sid = services.session_tokens.decode(_tok).session_id
                        except Exception:
                            _sid = ""
                        if _sid and _dg.has_open_stream(_sid):
                            await _dg.send_chunk(_sid, _wav)
                            query = await _dg.close_stream(_sid)

                    # Fallback to Whisper with the buffered WAV
                    if not query:
                        try:
                            query = await services.stt.transcribe_bytes(
                                _wav, filename="uplink.wav", lang_hint=_lang
                            )
                        except HTTPException as exc:
                            await _send_json_locked(
                                websocket, send_lock,
                                {"type": "error", "detail": exc.detail},
                            )
                            return
                    query = (query or "").strip()
                    if not query:
                        await _send_json_locked(
                            websocket, send_lock,
                            {"type": "error", "detail": "could not transcribe WebRTC audio"},
                        )
                        return
                    new_token = await _process_voice_turn(
                        websocket=websocket,
                        send_lock=send_lock,
                        services=services,
                        session_token=_tok,
                        query=query,
                        cancel_event=_cancel,
                        lang_hint=_lang,
                        peer_id=_pid,
                    )
                    current_session_token = new_token

                active_turn_task = asyncio.create_task(run_webrtc_audio_turn())

            elif msg_type == "audio_chunk_partial":
                if not services.settings.partial_stt_enabled or not current_session_token:
                    pass
                else:
                    _audio_b64 = str(payload.get("audio_b64", "")).strip()
                    _lang = str(payload.get("lang_hint", "en")).strip().lower()
                    if _lang not in {"hi", "en"}:
                        _lang = "en"
                    try:
                        _chunk_bytes = base64.b64decode(_audio_b64 + "==")
                    except Exception:
                        _chunk_bytes = b""
                    if len(_chunk_bytes) >= 600:
                        _dg: DeepgramSTTService | None = getattr(services, "deepgram_stt", None)
                        if _dg and _dg._client:
                            # Deepgram path: open a stream on first chunk, then feed subsequent chunks
                            try:
                                _sid = services.session_tokens.decode(current_session_token).session_id
                            except Exception:
                                _sid = ""
                            if _sid:
                                if not _dg.has_open_stream(_sid):
                                    asyncio.create_task(_dg.open_stream(_sid, _lang))
                                else:
                                    asyncio.create_task(_dg.send_chunk(_sid, _chunk_bytes))
                        else:
                            # OpenAI fallback: partial Whisper calls accumulated in Redis
                            _redis = getattr(services, "redis_store", None)
                            try:
                                _sid = services.session_tokens.decode(current_session_token).session_id
                            except Exception:
                                _sid = ""
                            if _sid and _redis and _redis._enabled:
                                async def _run_partial(_b=_chunk_bytes, _l=_lang, _s=_sid):
                                    partial = await services.stt.transcribe_partial(_b, "partial.webm", _l)
                                    if partial:
                                        existing = await _redis.get_partial(_s) or ""
                                        combined = f"{existing} {partial}".strip()[-400:]
                                        await _redis.set_partial(_s, combined, services.settings.partial_stt_chunk_ttl_sec)
                                asyncio.create_task(_run_partial())

            elif msg_type == "barge_in":
                await cancel_active_turn()
                # Close any open Deepgram stream — new utterance will open a fresh one
                _dg_bi: DeepgramSTTService | None = getattr(services, "deepgram_stt", None)
                if _dg_bi and current_session_token:
                    try:
                        _sid_bi = services.session_tokens.decode(current_session_token).session_id
                        if _dg_bi.has_open_stream(_sid_bi):
                            asyncio.create_task(_dg_bi.close_stream(_sid_bi))
                    except Exception:
                        pass
                await _send_json_locked(websocket, send_lock, {"type": "barge_in_ack"})

            elif msg_type == "ping":
                await _send_json_locked(websocket, send_lock, {"type": "pong"})

            else:
                await _send_json_locked(
                    websocket,
                    send_lock,
                    {"type": "error", "detail": f"unknown message type: {msg_type or '<empty>'}"},
                )
    except WebSocketDisconnect:
        pass
    finally:
        await cancel_active_turn()
