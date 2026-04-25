"""Deepgram STT service — batch + streaming (SDK v6).

Two modes:
  1. Batch (transcribe_bytes): drop-in replacement for OpenAI Whisper.

  2. Streaming: opens a Deepgram Live WebSocket per utterance.
     Each 500ms audio_chunk_partial is forwarded in real-time; by the time
     the user stops speaking the transcript is already mostly assembled.
     close_stream() returns the final accumulated transcript.

NOTE: The SDK v6 mis-encodes Python booleans as 'True'/'False' (Deepgram
returns HTTP 400). Streaming therefore uses direct websockets with a
hand-crafted URL. Batch transcription uses the SDK normally.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

try:
    from deepgram import DeepgramClient
    import websockets.sync.client as _wss
    _DG_AVAILABLE = True
except ImportError:
    _DG_AVAILABLE = False
    _log.warning("deepgram-sdk not installed — Deepgram STT unavailable")

from app.config import Settings


_NON_ALLOWED = re.compile(r"[\u1100-\u11FF\u3040-\u30FF\uAC00-\uD7FF\u4E00-\u9FFF]")
_DG_WS_BASE = "wss://api.deepgram.com/v1/listen"


# ---------------------------------------------------------------------------
# Streaming session state
# ---------------------------------------------------------------------------

@dataclass
class _StreamSession:
    finals: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    chunks: asyncio.Queue = field(default_factory=asyncio.Queue)
    lang_hint: str = "en"
    task: "asyncio.Task | None" = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class DeepgramSTTService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None
        self._api_key: str = ""
        self._sessions: dict[str, _StreamSession] = {}

        if not _DG_AVAILABLE:
            return
        if not settings.deepgram_api_key:
            _log.warning("DEEPGRAM_API_KEY not set — Deepgram STT disabled")
            return
        try:
            self._client = DeepgramClient(api_key=settings.deepgram_api_key)
            self._api_key = settings.deepgram_api_key
            _log.info("DeepgramSTTService ready (model=%s)", settings.deepgram_model)
        except Exception:
            _log.exception("Failed to initialise Deepgram client")

    # ------------------------------------------------------------------
    # Batch transcription — drop-in for STTService.transcribe_bytes
    # ------------------------------------------------------------------

    async def transcribe_bytes(
        self,
        content: bytes,
        filename: str = "audio.webm",
        lang_hint: str | None = None,
    ) -> str:
        if not self._client:
            raise RuntimeError("Deepgram client not initialised")
        if not content or len(content) < 600:
            raise ValueError("Audio too short")

        lang = self._resolve_lang(lang_hint)
        mime = _mime_from_filename(filename)

        def _sync_transcribe():
            response = self._client.listen.v1.media.transcribe_file(
                {"content": content, "mimetype": mime},
                model=self._settings.deepgram_model,
                language=lang,
                smart_format=True,
                punctuate=True,
            )
            return response.results.channels[0].alternatives[0].transcript or ""

        text = await asyncio.get_event_loop().run_in_executor(None, _sync_transcribe)
        text = (text or "").strip()
        if _NON_ALLOWED.search(text):
            return ""
        return text

    # ------------------------------------------------------------------
    # Streaming session management
    # ------------------------------------------------------------------

    async def open_stream(self, session_id: str, lang_hint: str | None = None) -> None:
        """Open a Deepgram Live WebSocket for this utterance."""
        if not self._api_key:
            return
        if session_id in self._sessions:
            await self.close_stream(session_id)

        lang = self._resolve_lang(lang_hint)
        session = _StreamSession(lang_hint=lang)
        self._sessions[session_id] = session

        async def _stream_worker():
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: self._run_sync_stream(session))
            except Exception:
                _log.exception("Deepgram stream worker failed for %s", session_id)
            finally:
                session.done.set()

        session.task = asyncio.create_task(_stream_worker())
        _log.debug("Deepgram stream opened for %s (lang=%s)", session_id, lang)

    def _build_ws_url(self, lang: str) -> str:
        """Build Deepgram streaming URL with properly lowercase-encoded booleans.

        The SDK v6 encodes Python booleans as 'True'/'False', which Deepgram
        rejects with HTTP 400. We hand-craft the URL string instead.
        """
        params = {
            "model": self._settings.deepgram_model,
            "language": lang,
            "smart_format": "true",
            "punctuate": "true",
            "interim_results": "false",
            "endpointing": "300",
        }
        return _DG_WS_BASE + "?" + urllib.parse.urlencode(params)

    def _run_sync_stream(self, session: _StreamSession) -> None:
        """Run the Deepgram streaming WebSocket in a thread via direct websockets."""
        loop = asyncio.new_event_loop()
        url = self._build_ws_url(session.lang_hint)
        headers = {"Authorization": f"Token {self._api_key}"}

        try:
            with _wss.connect(url, additional_headers=headers) as ws:

                def _listen():
                    try:
                        for raw in ws:
                            if isinstance(raw, bytes):
                                continue
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            msg_type = msg.get("type", "")
                            if msg_type == "Results":
                                try:
                                    alts = msg.get("channel", {}).get("alternatives", [{}])
                                    text = (alts[0].get("transcript") or "").strip()
                                    if text and msg.get("is_final"):
                                        session.finals.append(text)
                                except Exception:
                                    pass
                    except Exception:
                        pass  # connection closed by our side

                listen_thread = threading.Thread(target=_listen, daemon=True)
                listen_thread.start()

                # Feed chunks until sentinel (None)
                while True:
                    try:
                        chunk = loop.run_until_complete(
                            asyncio.wait_for(session.chunks.get(), timeout=0.1)
                        )
                    except asyncio.TimeoutError:
                        continue
                    if chunk is None:
                        break
                    ws.send(chunk)

                try:
                    ws.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass
                listen_thread.join(timeout=2.0)

        except Exception:
            _log.exception("Deepgram sync stream error")
        finally:
            loop.close()

    async def send_chunk(self, session_id: str, audio: bytes) -> None:
        session = self._sessions.get(session_id)
        if not session or not audio:
            return
        await session.chunks.put(audio)

    async def close_stream(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if not session:
            return ""
        await session.chunks.put(None)
        try:
            await asyncio.wait_for(session.done.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            _log.warning("Deepgram stream close timed out for %s", session_id)
        if session.task:
            session.task.cancel()

        transcript = " ".join(session.finals).strip()
        if _NON_ALLOWED.search(transcript):
            return ""
        _log.debug("Deepgram stream transcript: %r", transcript[:120])
        return transcript

    def has_open_stream(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close_stream(sid)

    def _resolve_lang(self, lang_hint: str | None) -> str:
        h = (lang_hint or "").strip().lower()
        if h == "hi":
            return "hi"
        return "en"


def _mime_from_filename(filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".mp4"):
        return "audio/mp4"
    if fn.endswith(".wav"):
        return "audio/wav"
    if fn.endswith(".ogg"):
        return "audio/ogg"
    return "audio/webm"
