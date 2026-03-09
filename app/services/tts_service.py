import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from openai import AsyncOpenAI

from app.config import Settings


class TTSService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    async def synthesize(self, text: str) -> str:
        if not self.client:
            raise HTTPException(status_code=500, detail="OpenAI API key is not configured for TTS")

        self.settings.audio_path.mkdir(parents=True, exist_ok=True)
        await self._cleanup_audio_cache()
        output_name = f"{uuid4().hex}.mp3"
        output_path = Path(self.settings.audio_path) / output_name

        speed = min(2.0, max(0.8, float(self.settings.openai_tts_speed or 1.0)))
        request_payload = {
            "model": self.settings.openai_tts_model,
            "voice": self.settings.openai_tts_voice,
            "input": text,
            "speed": speed,
        }
        instructions = (self.settings.openai_tts_instructions or "").strip()
        if instructions:
            request_payload["instructions"] = instructions

        try:
            async with self.client.audio.speech.with_streaming_response.create(**request_payload) as response:
                await response.stream_to_file(output_path)
        except TypeError:
            # Backward compatibility if SDK/model does not accept `instructions`.
            request_payload.pop("instructions", None)
            async with self.client.audio.speech.with_streaming_response.create(**request_payload) as response:
                await response.stream_to_file(output_path)

        return f"/static/audio/{output_name}"

    async def _cleanup_audio_cache(self) -> None:
        retention = max(10, int(self.settings.tts_retention_minutes))
        max_files = max(50, int(self.settings.tts_max_files))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=retention)
        audio_dir = Path(self.settings.audio_path)

        def _cleanup() -> None:
            files = sorted(
                [p for p in audio_dir.glob("*.mp3") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in files:
                try:
                    modified = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                    if modified < cutoff:
                        p.unlink(missing_ok=True)
                except OSError:
                    continue
            files = sorted(
                [p for p in audio_dir.glob("*.mp3") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in files[max_files:]:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    continue

        await asyncio.to_thread(_cleanup)
