# Multilingual Voice AI Movie Assistant (Track B)

Backend implementation for:
- FastAPI API layer
- RAG-style movie retrieval (FAISS + fallback retriever)
- LLM abstraction (OpenAI or fine-tuned endpoint)
- Voice pipeline (STT + TTS)
- Minimal Tailwind UI
- Docker + Render deployment setup

## Project Structure

```text
app/
  main.py
  config.py
  routes/
    recommend.py
    voice.py
  services/
    retriever.py
    llm_service.py
    stt_service.py
    tts_service.py
  models/
    schemas.py
  static/
    index.html
    script.js
```

## API

### `POST /recommend`

Request:

```json
{
  "query": "suggest emotional sci-fi movies",
  "include_audio": false
}
```

Response:

```json
{
  "text": "...",
  "movies": [
    {
      "title": "Interstellar",
      "overview": "...",
      "genres": ["Adventure", "Drama", "Science Fiction"],
      "top_actors": ["Matthew McConaughey", "Anne Hathaway", "Jessica Chastain"],
      "director": "Christopher Nolan",
      "poster_url": "https://image.tmdb.org/t/p/w500/..."
    }
  ],
  "audio_url": null
}
```

### `POST /voice-chat`

Form-data field:
- `audio`: file

Response:

```json
{
  "text": "...",
  "audio_url": "/static/audio/<file>.mp3",
  "movies": []
}
```

## Local Run

1. Copy `.env.example` to `.env` and set keys.
2. Install deps:

```bash
pip install -r requirements.txt
```

3. Run:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t movie-assistant .
docker run --env-file .env -p 8000:8000 movie-assistant
```

## Render

- Connect repo to Render
- Use Docker deploy mode (`render.yaml` included)
- Set env vars from `.env.example`
- Start command comes from Dockerfile (`gunicorn + uvicorn worker`)
- Set `OPENAI_API_KEY` in Render dashboard (not in repo)
- Keep `WEB_CONCURRENCY=1` on free/low-memory instances

## Security Notes

- Set `APP_ENV=production` in production.
- Set a strong `SESSION_JWT_SECRET` (>= 32 chars) in production.
- Configure `ALLOWED_HOSTS` to include your Render hostname.
- Optional: set `APP_API_KEY` to protect `/recommend`, `/voice-chat`, and `/start-voice-session`.
- If `APP_API_KEY` is enabled for browser testing, open UI with `?api_key=<value>` so the frontend can send it.
- Tune request limits with:
  - `MAX_QUERY_CHARS`
  - `MAX_AUDIO_BYTES`
  - `RATE_LIMIT_*` variables
