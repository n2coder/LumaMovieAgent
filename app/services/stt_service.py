import re

from fastapi import HTTPException, UploadFile
from openai import AsyncOpenAI, BadRequestError

from app.config import Settings


class STTService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    async def _transcribe_content(self, content: bytes, filename: str) -> str:
        if not self.client:
            raise HTTPException(status_code=500, detail="OpenAI API key is not configured for STT")

        if len(content) > self.settings.max_audio_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Audio too large. Max allowed is {self.settings.max_audio_bytes} bytes.",
            )
        if not content or len(content) < 1200:
            raise HTTPException(status_code=400, detail="Audio too short. Please speak for at least 1 second.")
        try:
            response = await self.client.audio.transcriptions.create(
                model=self.settings.openai_stt_model,
                file=(filename or "audio.webm", content),
            )
        except BadRequestError as exc:
            raise HTTPException(status_code=400, detail="Invalid audio input.") from exc
        text = getattr(response, "text", "")
        text = text.strip()

        # If Hindi speech is transcribed in Perso-Arabic/Urdu script, convert to Devanagari.
        if re.search(r"[\u0600-\u06FF]", text):
            try:
                fix = await self.client.chat.completions.create(
                    model=self.settings.openai_chat_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Convert input to Devanagari Hindi script only. Keep meaning same. "
                                "Return only converted text."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    temperature=0,
                )
                text = (fix.choices[0].message.content or text).strip()
            except Exception:
                pass
        return text

    async def transcribe(self, audio_file: UploadFile) -> str:
        content = await audio_file.read()
        return await self._transcribe_content(content=content, filename=audio_file.filename or "audio.webm")

    async def transcribe_bytes(self, content: bytes, filename: str = "audio.webm") -> str:
        return await self._transcribe_content(content=content, filename=filename)
