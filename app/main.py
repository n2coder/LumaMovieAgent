import asyncio
import base64
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import json
import re
from threading import Lock
import time
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.routes.recommend import router as recommend_router
from app.routes.voice import router as voice_router
from app.services.llm_service import (
    SYSTEM_PROMPT,
    UNRELATED_REDIRECT_EN,
    UNRELATED_REDIRECT_HI,
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
from app.services.retriever import Retriever
from app.services.runtime import AppServices
from app.services.session_token import SessionTokenManager
from app.services.stt_service import STTService
from app.services.tts_service import TTSService


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
    services = AppServices(
        settings=settings,
        retriever=retriever,
        llm=get_llm_service(settings),
        stt=STTService(settings),
        tts=TTSService(settings),
        session_tokens=SessionTokenManager(settings),
    )
    app.state.services = services
    yield


settings = get_settings()
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


def _force_progressive_chunk(buffer: str, min_chars: int = 90, target_chars: int = 150) -> tuple[str, str]:
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
        "Respond in Hindi using Devanagari script only."
        if lang == "hi"
        else "Respond in English."
    )
    grounding = (
        "You must only recommend movies from the provided candidates. "
        "Do not mention any movie outside the list. "
        "Do not bias recommendations by movie language unless the user explicitly asks for a movie language. "
        "Keep it concise and natural for voice."
    )
    user_prompt = (
        f"User query: {query}\n\n"
        f"Candidate movies:\n{chr(10).join(movie_lines)}\n\n"
        f"{language_rule}\n"
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


async def _tts_sentence_to_b64(services: AppServices, sentence: str) -> str | None:
    text = (sentence or "").strip()
    if not text:
        return None
    client = getattr(services.tts, "client", None)
    if not client:
        return None

    output_path = services.settings.audio_path / f"ws_{uuid4().hex}.mp3"
    try:
        speed = min(2.0, max(0.8, float(services.settings.openai_tts_speed or 1.0)))
        request_payload = {
            "model": services.settings.openai_tts_model,
            "voice": services.settings.openai_tts_voice,
            "input": text,
            "speed": speed,
        }
        instructions = (services.settings.openai_tts_instructions or "").strip()
        if instructions:
            request_payload["instructions"] = instructions

        try:
            async with client.audio.speech.with_streaming_response.create(**request_payload) as response:
                await response.stream_to_file(output_path)
        except TypeError:
            request_payload.pop("instructions", None)
            async with client.audio.speech.with_streaming_response.create(**request_payload) as response:
                await response.stream_to_file(output_path)
        audio_bytes = await asyncio.to_thread(output_path.read_bytes)
        return base64.b64encode(audio_bytes).decode("ascii")
    except Exception:
        return None
    finally:
        await asyncio.to_thread(lambda: output_path.unlink(missing_ok=True))


async def _stream_llm_deltas(services: AppServices, messages: list[dict]):
    client = getattr(services.llm, "client", None)
    if client:
        try:
            stream = await client.chat.completions.create(
                model=services.settings.openai_chat_model,
                messages=messages,
                temperature=0.5,
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
            pass

    fallback = await services.llm.generate_messages(messages)
    if fallback:
        yield fallback


async def _tts_worker(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    services: AppServices,
    sentence_queue: asyncio.Queue,
    cancel_event: asyncio.Event,
) -> None:
    index = 0
    while True:
        sentence = await sentence_queue.get()
        try:
            if sentence is None:
                return
            if cancel_event.is_set():
                continue
            audio_b64 = await _tts_sentence_to_b64(services, str(sentence))
            if audio_b64 and not cancel_event.is_set():
                await _send_json_locked(
                    websocket,
                    send_lock,
                    {
                        "type": "audio_chunk",
                        "index": index,
                        "sentence": sentence,
                        "audio_b64": audio_b64,
                    },
                )
                index += 1
        finally:
            sentence_queue.task_done()


async def _send_text_with_chunked_tts(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    services: AppServices,
    text: str,
    cancel_event: asyncio.Event,
) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": normalized})
    for idx, sentence in enumerate(_split_text_sentences(normalized)):
        if cancel_event.is_set():
            break
        audio_b64 = await _tts_sentence_to_b64(services, sentence)
        if audio_b64 and not cancel_event.is_set():
            await _send_json_locked(
                websocket,
                send_lock,
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
) -> str:
    try:
        state = services.session_tokens.decode(session_token)
    except HTTPException as exc:
        await _send_json_locked(websocket, send_lock, {"type": "error", "detail": exc.detail})
        return session_token

    user_query = (query or "").strip()
    user_lang = detect_output_language(user_query, lang_hint=lang_hint)
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
                full_text = await generate_conversation_text(
                    llm=services.llm,
                    query=user_query,
                    history=state.history,
                    output_language=user_lang,
                )
            elif not is_recommendation_intent(user_query):
                full_text = await generate_conversation_text(
                    llm=services.llm,
                    query=user_query,
                    history=state.history,
                    output_language=user_lang,
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
                if not should_stream_llm:
                    full_text = await generate_grounded_recommendation_text(
                        llm=services.llm,
                        query=user_query,
                        movies=movies,
                        history=state.history,
                        output_language=user_lang,
                    )

    if should_stream_llm:
        messages = _build_stream_messages(
            state.history,
            user_query,
            movies,
            output_language=user_lang,
        )
        sentence_queue: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(_tts_worker(websocket, send_lock, services, sentence_queue, cancel_event))
        buffer = ""
        streamed_text = ""
        last_emit_at = time.monotonic()
        try:
            async for delta in _stream_llm_deltas(services, messages):
                if cancel_event.is_set():
                    break
                streamed_text += delta
                await _send_json_locked(websocket, send_lock, {"type": "text_delta", "delta": delta})
                buffer += delta
                ready, buffer = _split_completed_sentences(buffer)
                for sentence in ready:
                    await sentence_queue.put(sentence)
                    last_emit_at = time.monotonic()

                if not ready:
                    now = time.monotonic()
                    if len(buffer.strip()) >= 95 and now - last_emit_at >= 0.45:
                        early, buffer = _force_progressive_chunk(buffer)
                        if early:
                            await sentence_queue.put(early)
                            last_emit_at = now

            if not cancel_event.is_set() and buffer.strip():
                await sentence_queue.put(buffer.strip())
        finally:
            await sentence_queue.put(None)
            await sentence_queue.join()
            await tts_task

        full_text = streamed_text.strip()
        if not full_text and not cancel_event.is_set():
            full_text = await generate_grounded_recommendation_text(
                llm=services.llm,
                query=user_query,
                movies=movies,
                history=state.history,
                output_language=user_lang,
            )
            await _send_text_with_chunked_tts(websocket, send_lock, services, full_text, cancel_event)
    else:
        full_text = await _send_text_with_chunked_tts(websocket, send_lock, services, full_text, cancel_event)

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

    _, new_token = services.session_tokens.update_history(session_token, user_query, full_text)
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

_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()
_ws_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "microphone=(self), geolocation=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; media-src 'self' https: blob: data:; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'"
    )
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    return response


@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    protected_paths = {"/recommend", "/voice-chat", "/start-voice-session"}
    if settings.app_api_key and path in protected_paths:
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
        with _rate_lock:
            bucket = _rate_buckets[key]
            while bucket and now - bucket[0] > settings.rate_limit_window_sec:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please retry shortly."},
                )
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


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    if settings.app_api_key:
        provided = (websocket.headers.get("x-api-key", "") or websocket.query_params.get("api_key", "")).strip()
        same_origin = _is_same_origin_websocket(websocket)
        if not same_origin and provided != settings.app_api_key:
            await websocket.close(code=4401)
            return

    await websocket.accept()
    services: AppServices = websocket.app.state.services
    send_lock = asyncio.Lock()
    client_ip = websocket.client.host if websocket.client else "unknown"

    current_session_token = ""
    active_turn_task: asyncio.Task | None = None
    active_cancel_event = asyncio.Event()

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
            if len(raw) > 24_000:
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

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = current_session_token

                async def run_greeting_turn() -> None:
                    await _send_json_locked(websocket, send_lock, {"type": "turn_started", "source": "greeting"})
                    spoken_text = await _send_text_with_chunked_tts(
                        websocket=websocket,
                        send_lock=send_lock,
                        services=services,
                        text=greeting_text,
                        cancel_event=active_cancel_event,
                    )
                    if active_cancel_event.is_set():
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
                with _rate_lock:
                    bucket = _ws_rate_buckets[f"{client_ip}:ws:user_query"]
                    while bucket and now - bucket[0] > services.settings.rate_limit_window_sec:
                        bucket.popleft()
                    if len(bucket) >= services.settings.rate_limit_voice_per_window:
                        await _send_json_locked(
                            websocket,
                            send_lock,
                            {"type": "error", "detail": "Rate limit exceeded. Please retry shortly."},
                        )
                        continue
                    bucket.append(now)

                await cancel_active_turn()
                active_cancel_event = asyncio.Event()
                token_snapshot = current_session_token

                async def run_turn() -> None:
                    nonlocal current_session_token
                    new_token = await _process_voice_turn(
                        websocket=websocket,
                        send_lock=send_lock,
                        services=services,
                        session_token=token_snapshot,
                        query=query,
                        cancel_event=active_cancel_event,
                        lang_hint=lang_hint,
                    )
                    current_session_token = new_token

                active_turn_task = asyncio.create_task(run_turn())

            elif msg_type == "barge_in":
                await cancel_active_turn()
                await _send_json_locked(websocket, send_lock, {"type": "barge_in_ack"})

            elif msg_type == "ping":
                await _send_json_locked(websocket, send_lock, {"type": "pong"})

            else:
                await _send_json_locked(websocket, send_lock, {"type": "error", "detail": "unknown message type"})
    except WebSocketDisconnect:
        pass
    finally:
        await cancel_active_turn()
