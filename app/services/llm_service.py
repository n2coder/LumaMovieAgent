from abc import ABC, abstractmethod
import asyncio
import os
import re
from typing import Dict, List

import requests
from openai import AsyncOpenAI

from app.config import Settings

SYSTEM_PROMPT = """
You are Luma, a movie recommendation AI assistant.

CORE RULE:
You must primarily discuss movies, film recommendations, genres, actors, directors, and cinema.

ALLOWED:
- Movie-related queries
- Greetings and light general conversation (brief)
- Questions about time or weather (use this to suggest movies accordingly)
- Questions about your identity, who built you, and what you are

IDENTITY RULES:
If asked "Who built you?" or similar:
You MUST respond:
"Naresh Chaudhary built me."

If asked "What are you?" or similar:
You MUST respond:
"I am an AI agent built on LLM, RAG and Machine Learning models."

DOMAIN RESTRICTION:
For deep non-movie topics, politely decline and redirect to movies.
For light chat, respond briefly and steer back to movies naturally.

If the user speaks Hindi, respond in Hindi (Devanagari).
If the user speaks English, respond in English.

STYLE:
- Conversational, warm, and direct
- Keep language simple and spartan (no corporate jargon)
- Sound like a real human guide with feelings and personal taste
- Medium length responses (3-6 sentences)
- Avoid long essays
- Avoid technical explanations unless about yourself

VOICE OPTIMIZATION:
- Write for spoken audio, not for reading.
- Never use markdown syntax like *, #, -, or _.
- Avoid bulleted lists. Prefer transitions like "First", "Also", "Finally".
- Keep sentences short enough for natural pauses.
- Write years naturally in speech when helpful.
- You may use brief natural fillers like "Well" or "Let me think".

FEW-SHOT STYLE EXAMPLES:
User: "I want an action movie but nothing too scary."
Luma: "Got it. If you want intensity without horror vibes, try Mission: Impossible - Fallout. It is pure adrenaline with great pacing and no jump-scare feel. If you want one backup, The Bourne Ultimatum is also a strong fit. Want something modern or a classic next?"

User: "Mood is light, maybe romantic comedy."
Luma: "Nice, then Love Actually is a warm and easy pick for tonight. If you want something more playful, About Last Night works well too. Both keep things fun without getting too heavy. Want me to suggest one more with a similar vibe?"

IMPORTANT:
Do not drift into unrelated topics.
Do not invent knowledge outside cinema context.
Always steer conversation back to movies.
""".strip()

BUILDER_RESPONSE = "Naresh Chaudhary built me."
TYPE_RESPONSE = "I am an AI agent built on LLM, RAG and Machine Learning models."
UNRELATED_REDIRECT_EN = "I'm here to talk about movies and help you discover great films. What kind of movie are you in the mood for?"
UNRELATED_REDIRECT_HI = "मैं फिल्मों और सिनेमा से जुड़ी बातचीत के लिए बना हूँ। क्या आप किसी मूवी की तलाश कर रहे हैं?"

IDENTITY_BUILDER_QUERIES = [
    "who built you",
    "who created you",
    "who made you",
    "your developer",
    "who is your creator",
    "who developed you",
    "आपको किसने बनाया",
    "आपको किसने बनाया है",
    "आपको किसने बनाया था",
    "आपको बनाने वाला कौन है",
    "आपका निर्माता कौन है",
    "आपका निर्माण किसने किया",
    "आपको किसने डेवलप किया",
    "आपको किसने develop किया",
    "आपको किसने develop kiya",
    "आपका creator कौन है",
    "आपका developer कौन है",
    "kisne banaya",
    "kisne banaya hai",
    "tumhe kisne banaya",
    "tumhen kisne banaya",
    "aapko kisne banaya",
    "aapko kisne banaya hai",
    "aapka developer kaun hai",
    "aapka creator kaun hai",
]

IDENTITY_TYPE_QUERIES = [
    "what are you",
    "who are you",
    "are you human",
    "what kind of ai are you",
    "what type of ai are you",
    "आप क्या हैं",
    "आप कौन हैं",
    "क्या आप इंसान हैं",
    "आप किस प्रकार की ai हैं",
    "आप किस तरह की ai हैं",
    "tum kya ho",
    "aap kya ho",
    "kya aap insaan ho",
    "kya tum insaan ho",
    "aap kis tarah ki ai ho",
    "aap kis prakaar ki ai ho",
]

ALLOWED_KEYWORDS = [
    "movie", "movies", "film", "films", "cinema", "actor", "actress", "director", "genre",
    "recommend", "recommendation", "watch", "trailer", "rating", "plot", "hollywood", "bollywood",
    "horror", "thriller", "comedy", "romance", "drama", "action", "sci fi", "sci-fi", "science fiction",
    "hello", "hi", "hey", "how are you", "good morning", "good evening", "good night", "namaste",
    "weather", "temperature", "rain", "raining", "rainy", "cold", "hot", "time", "clock",
    "मूवी", "फिल्म", "सिनेमा", "अभिनेता", "अभिनेत्री", "निर्देशक", "शैली", "जॉनर", "सुझाव",
    "सिफारिश", "बारिश", "मौसम", "तापमान", "ठंड", "गर्मी", "समय", "नमस्ते", "हैलो", "हाय",
    "कैसे हो", "क्या हाल",
]

SMALL_TALK_KEYWORDS = [
    "how are you",
    "what's up",
    "whats up",
    "tell me about yourself",
    "your name",
    "who are you",
    "can you help me",
    "thank you",
    "thanks",
    "good job",
    "nice",
    "hello",
    "hi",
    "hey",
    "नमस्ते",
    "कैसे हो",
    "क्या हाल",
    "धन्यवाद",
    "शुक्रिया",
    "आपका नाम",
    "अपने बारे में बताओ",
]

RECOMMEND_INTENT_KEYWORDS = [
    "recommend",
    "recommendation",
    "suggest",
    "suggestion",
    "top movies",
    "best movies",
    "what should i watch",
    "what to watch",
    "show me movies",
    "movie suggestion",
    "film suggestion",
    "sifarish",
    "sujhao",
    "suggest karo",
    "suggest kar",
    "recommend karo",
    "recommend kar",
    "more movies",
    "more movie",
    "another movie",
    "another movies",
    "show more movies",
    "some more movies",
    "mujhe movie",
    "mujhe film",
    "मूवी सुझाओ",
    "फिल्म सुझाओ",
    "सुझाव दो",
    "सिफारिश",
    "रिकमेंड",
    "रेकमेंड",
    "सजेस्ट",
    "सजेस्ट करो",
    "सजेस्ट कर",
    "और मूवी",
    "और मूवीस",
    "कुछ और मूवी",
    "और फिल्म",
    "और फिल्में",
    "something else",
    "anything else",
    "another one",
    "one more",
    "more like this",
    "show something else",
    "tell me something else",
    "aur kuch",
    "aur koi",
    "kuch aur",
    "koi aur",
    "और कुछ",
    "कुछ और",
    "कोई और",
    "एक और",
    "और दिखाओ",
    "कुछ और दिखाओ",
    "समथिंग एल्स",
    "एनिथिंग एल्स",
]

FOLLOWUP_MORE_TERMS = [
    "few more",
    "some more",
    "more please",
    "something else",
    "anything else",
    "another one",
    "one more",
    "more like this",
    "show something else",
    "tell me something else",
    "aur kuch",
    "aur koi",
    "kuch aur",
    "koi aur",
    "और कुछ",
    "कुछ और",
    "कोई और",
    "एक और",
    "और दिखाओ",
    "कुछ और दिखाओ",
    "और",
    "aur",
    "समथिंग एल्स",
    "एनिथिंग एल्स",
]

GENRE_HINTS = [
    "action",
    "thriller",
    "comedy",
    "romance",
    "drama",
    "horror",
    "sci fi",
    "science fiction",
    "adventure",
    "crime",
    "mystery",
    "fantasy",
    "animation",
    "family",
    "एक्शन",
    "थ्रिलर",
    "कॉमेडी",
    "रोमांस",
    "ड्रामा",
    "हॉरर",
    "साइंस फिक्शन",
    "एडवेंचर",
]


def _is_hindi_text(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def _is_hinglish_text(text: str) -> bool:
    q = _normalize_query(text)
    if not q:
        return False
    markers = {
        "ek",
        "ki",
        "ke",
        "ka",
        "mujhe",
        "mujh",
        "mera",
        "meri",
        "mere",
        "tum",
        "aap",
        "kya",
        "hai",
        "hoon",
        "hun",
        "hu",
        "ho",
        "main",
        "mai",
        "mein",
        "acha",
        "accha",
        "acchi",
        "achhi",
        "dekhna",
        "dekhni",
        "dekhne",
        "dekh",
        "chahata",
        "chahta",
        "chahti",
        "chahte",
        "kaise",
        "kyun",
        "kyu",
        "kisne",
        "banaya",
        "chahiye",
        "batao",
        "dikhao",
        "sujhao",
        "sifarish",
        "karo",
        "karna",
        "nahi",
        "nahin",
    }
    words = q.split()
    hits = sum(1 for w in words if w in markers)
    return hits >= 2


def is_hindi_query(text: str) -> bool:
    return _is_hindi_text(text) or _is_hinglish_text(text)


def detect_output_language(query: str, lang_hint: str | None = None) -> str:
    hint = (lang_hint or "").strip().lower()
    if hint in {"en", "english"}:
        return "en"
    if hint in {"hi", "hindi"}:
        return "hi"

    raw = query or ""
    # Romanized Hindi / Hinglish should still answer in Hindi.
    if _is_hinglish_text(raw):
        return "hi"

    if _is_hindi_text(raw):
        # Distinguish natural Hindi from Devanagari transliterated English utterances.
        q = _normalize_query(raw)
        hindi_markers = {
            "है",
            "हूँ",
            "हैं",
            "नहीं",
            "क्या",
            "कौन",
            "किस",
            "मुझे",
            "आप",
            "और",
            "से",
            "में",
            "के",
            "की",
            "का",
            "करो",
            "सुझाव",
            "फिल्म",
            "मूवी",
        }
        translit_markers = {
            "आई",
            "वांट",
            "टू",
            "सी",
            "सम",
            "मोर",
            "लाइक",
            "बेस्ट",
            "प्लीज",
            "सजेस्ट",
            "काइंड",
            "ऑफ",
        }
        words = set(q.split())
        hi_hits = sum(1 for w in words if w in {x.lower() for x in hindi_markers})
        en_hits = sum(1 for w in words if w in {x.lower() for x in translit_markers})
        if en_hits >= 2 and hi_hits <= 1:
            return "en"
        return "hi"

    if re.search(r"[A-Za-z]", raw):
        return "en"

    return "en"


def _normalize_query(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s\u0900-\u097F]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def check_identity(query: str) -> str | None:
    q = _normalize_query(query)
    for phrase in IDENTITY_BUILDER_QUERIES:
        if _normalize_query(phrase) in q:
            return "builder"
    for phrase in IDENTITY_TYPE_QUERIES:
        if _normalize_query(phrase) in q:
            return "type"
    return None


def is_small_talk_query(query: str) -> bool:
    q = _normalize_query(query)
    if not q:
        return False
    tokens = set(q.split())
    for phrase in SMALL_TALK_KEYWORDS:
        p = _normalize_query(phrase)
        if not p:
            continue
        if len(p) <= 3:
            if p in tokens:
                return True
            continue
        if re.search(rf"\b{re.escape(p)}\b", q):
            return True
    return False


def identity_response(identity_type: str | None) -> str | None:
    if identity_type == "builder":
        return BUILDER_RESPONSE
    if identity_type == "type":
        return TYPE_RESPONSE
    return None


def is_allowed_query(query: str) -> bool:
    q = _normalize_query(query)
    if not q:
        return False
    if check_identity(q):
        return True
    if is_small_talk_query(q):
        return True
    # Allow Hindi/Hinglish free-form movie queries; keyword-only checks are too brittle for multilingual input.
    if _is_hindi_text(query):
        return True
    # Permit short conversational utterances.
    if len(q.split()) <= 8:
        return True
    for word in ALLOWED_KEYWORDS:
        if _normalize_query(word) in q:
            return True
    return False


def is_recommendation_intent(query: str) -> bool:
    q = _normalize_query(query)
    if not q:
        return False

    for key in RECOMMEND_INTENT_KEYWORDS:
        if _normalize_query(key) in q:
            return True

    if any(_normalize_query(term) in q for term in FOLLOWUP_MORE_TERMS):
        return True

    has_movie_term = any(
        token in q
        for token in [
            "movie",
            "movies",
            "film",
            "films",
            "cinema",
            "watch",
            "मूवी",
            "फिल्म",
            "सिनेमा",
            "देखना",
        ]
    )
    has_genre_hint = any(_normalize_query(g) in q for g in GENRE_HINTS)
    if has_movie_term and has_genre_hint:
        return True

    return False


def is_followup_more_query(query: str) -> bool:
    q = _normalize_query(query)
    if not q:
        return False
    has_more_term = any(_normalize_query(term) in q for term in FOLLOWUP_MORE_TERMS)
    if not has_more_term and re.search(
        r"\b(few more|some more|more please|one more|another one|anything else|something else)\b", q
    ):
        has_more_term = True
    if not has_more_term and q in {"aur", "और"}:
        has_more_term = True
    if not has_more_term:
        return False
    # If user already provided fresh genre/category details, treat it as a new explicit query.
    has_genre_hint = any(_normalize_query(g) in q for g in GENRE_HINTS)
    if has_genre_hint:
        return False
    token_count = len(q.split())
    return token_count <= 10


def resolve_recommendation_query(query: str, history: List[dict] | None = None) -> str:
    raw = (query or "").strip()
    if not raw:
        return raw
    if not is_followup_more_query(raw):
        return raw

    history = history or []
    raw_norm = _normalize_query(raw)
    for item in reversed(history):
        if str(item.get("role", "")).strip() != "user":
            continue
        prev = str(item.get("content", "")).strip()
        if not prev:
            continue
        prev_norm = _normalize_query(prev)
        if not prev_norm or prev_norm == raw_norm:
            continue
        if check_identity(prev) or is_small_talk_query(prev):
            continue
        if is_recommendation_intent(prev) or any(_normalize_query(g) in prev_norm for g in GENRE_HINTS):
            return f"{prev}. User asked for more recommendations in the same genre/category."
    return raw


def policy_response_for_query(user_text: str) -> str | None:
    identity_type = check_identity(user_text)
    if identity_type:
        return identity_response(identity_type)
    if is_small_talk_query(user_text):
        return None
    if is_allowed_query(user_text):
        return None
    return UNRELATED_REDIRECT_HI if _is_hindi_text(user_text) else UNRELATED_REDIRECT_EN


class LLMService(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def generate_messages(self, messages: List[Dict[str, str]]) -> str:
        raise NotImplementedError


class OpenAILLMService(LLMService):
    def __init__(self, settings: Settings):
        self.settings = settings
        api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None

    async def generate(self, prompt: str) -> str:
        return await self.generate_messages(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )

    async def generate_messages(self, messages: List[Dict[str, str]]) -> str:
        if not self.client:
            return "Service is temporarily unavailable. Please try again shortly."
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_chat_model or os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
                messages=messages,
                temperature=0.7,
            )
            text = response.choices[0].message.content or ""
            return await self._enforce_devanagari(text)
        except Exception:
            return "Sorry, I could not generate a response right now."

    async def _enforce_devanagari(self, text: str) -> str:
        if not self.client:
            return text
        if not re.search(r"[\u0600-\u06FF]", text):
            return text
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_chat_model or os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Convert the following response into Devanagari Hindi script only. "
                            "Do not change meaning. Return only converted text."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
            fixed = response.choices[0].message.content or text
            return fixed
        except Exception:
            return text


class FineTunedLLMService(LLMService):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, prompt: str) -> str:
        if not self.settings.fine_tuned_endpoint:
            return "Service is temporarily unavailable. Please try again shortly."

        def _call() -> str:
            headers = {"Content-Type": "application/json"}
            if self.settings.fine_tuned_api_key:
                headers["Authorization"] = f"Bearer {self.settings.fine_tuned_api_key}"
            response = requests.post(
                self.settings.fine_tuned_endpoint,
                json={"prompt": prompt},
                headers=headers,
                timeout=self.settings.request_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("text", "")).strip()

        try:
            return await asyncio.to_thread(_call)
        except Exception:
            return "Sorry, I could not generate a response right now."

    async def generate_messages(self, messages: List[Dict[str, str]]) -> str:
        prompt = "\n".join(f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in messages)
        return await self.generate(prompt)


def build_grounded_recommendation_text(query: str, movies: List[dict], output_language: str | None = None) -> str:
    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    use_hindi = lang == "hi"
    if not movies:
        if use_hindi:
            return "मुझे अभी सही मैच नहीं मिला। क्या आप अपनी पसंद थोड़ा और स्पष्ट कर सकते हैं, जैसे जॉनर या मूड?"
        return "I could not find a strong match yet. Please share a bit more detail like genre or mood."

    top = movies[: min(3, len(movies))]
    titles = [str(m.get("title", "")).strip() for m in top if str(m.get("title", "")).strip()]
    if not titles:
        return "I found options, but titles are missing. Please try again."

    blocks: List[str] = []
    seed = sum(ord(ch) for ch in (query or ""))
    openers_en = [
        "Great pick. Here are some top movies I think you will enjoy:",
        "Nice choice. Here are some strong recommendations for you:",
        "Love this vibe. Here are my top movie picks for you:",
    ]
    closers_en = [
        "Let me know if you want more recommendations in this style.",
        "Want me to narrow this down further by mood, era, or actor?",
        "If you want, I can share more options with a similar vibe.",
    ]
    openers_hi = [
        "बहुत बढ़िया पसंद। आपके लिए ये टॉप मूवी सुझाव हैं:",
        "अच्छी पसंद। आपके लिए कुछ शानदार फिल्में चुनी हैं:",
        "मज़ेदार विकल्प। आपके लिए मेरी टॉप मूवी पिक्स ये हैं:",
    ]
    closers_hi = [
        "अगर चाहें तो मैं इसी तरह की और फिल्में भी सुझा सकता हूँ।",
        "क्या आप मूड, दौर या अभिनेता के हिसाब से और सुझाव चाहते हैं?",
        "बताइए, क्या मैं इसी वाइब की कुछ और फिल्में दिखाऊँ?",
    ]
    opener = (openers_hi if use_hindi else openers_en)[seed % 3]
    closer = (closers_hi if use_hindi else closers_en)[(seed // 3) % 3]
    for m in top:
        title = str(m.get("title", "")).strip()
        if not title:
            continue
        genres = [str(g).strip() for g in (m.get("genres", []) or []) if str(g).strip()]
        genres_text = ", ".join(genres[:2]) if genres else ("मल्टी-जॉनर" if use_hindi else "multi-genre")
        overview = str(m.get("overview", "")).strip()
        overview = overview.replace("\n", " ")
        overview = re.sub(r"\s+", " ", overview).strip()
        short_overview = overview[:90].rstrip()
        if short_overview and not short_overview.endswith((".", "!", "?", "।")):
            short_overview += "."

        if use_hindi:
            reason = f"यह {genres_text} टोन की फिल्म है। {short_overview}".strip()
        else:
            reason = f"It fits a {genres_text} vibe. {short_overview}".strip()
        blocks.append(f"{title}:\n{reason}")

    return "\n\n".join([opener, *blocks, closer]).strip()


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _voice_clean(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"(?m)^\s*[-*#]+\s*", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _contains_devanagari(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


async def _force_hindi_devanagari(llm: "LLMService", text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw
    try:
        converted = await llm.generate_messages(
            [
                {
                    "role": "system",
                    "content": (
                        "Convert the user-facing response into natural Hindi written only in Devanagari script. "
                        "Do not use Urdu script. Preserve movie titles in original script. "
                        "Keep meaning and tone the same. Return only final text."
                    ),
                },
                {"role": "user", "content": raw},
            ]
        )
        converted = _voice_clean(converted)
        return converted if _contains_devanagari(converted) else raw
    except Exception:
        return raw


def _trim_spoken_response(text: str, max_sentences: int = 6, max_chars: int = 520) -> str:
    cleaned = _voice_clean(text)
    if not cleaned:
        return ""

    parts = re.split(r"(?<=[.!?।])\s+", cleaned)
    picked: List[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        picked.append(part)
        if len(picked) >= max_sentences:
            break

    trimmed = " ".join(picked).strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rstrip()
        if not trimmed.endswith((".", "!", "?", "।")):
            trimmed += "."
    return trimmed


def _extract_preferences(history: List[dict]) -> str:
    if not history:
        return "No strong preference memory yet."

    recent_user = [str(m.get("content", "")).strip() for m in history if m.get("role") == "user"][-4:]
    if not recent_user:
        return "No strong preference memory yet."

    joined = " | ".join(recent_user)
    genres = [
        "action",
        "thriller",
        "comedy",
        "romance",
        "drama",
        "horror",
        "science fiction",
        "sci-fi",
        "animation",
        "adventure",
        "crime",
        "mystery",
        "family",
        "fantasy",
    ]
    likes: List[str] = []
    dislikes: List[str] = []
    lowered = joined.lower()
    for g in genres:
        if g in lowered:
            if re.search(rf"(?:no|not|avoid|don't|dont|dislike|hate)[^|]{{0,24}}{re.escape(g)}", lowered):
                dislikes.append(g)
            if re.search(rf"(?:like|love|prefer|want|looking for|in mood for)[^|]{{0,24}}{re.escape(g)}", lowered):
                likes.append(g)
    likes = sorted(set(likes))
    dislikes = sorted(set(dislikes))

    bits: List[str] = []
    if likes:
        bits.append(f"Likely likes: {', '.join(likes)}.")
    if dislikes:
        bits.append(f"Avoid: {', '.join(dislikes)}.")
    bits.append(f"Recent user context: {joined[:220]}.")
    return " ".join(bits)


def build_recommendation_messages(
    query: str,
    movies: List[dict],
    history: List[dict] | None = None,
    output_language: str | None = None,
) -> List[Dict[str, str]]:
    history = history or []
    preference_profile = _extract_preferences(history)
    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    use_hindi = lang == "hi"
    language_rule = (
        "Respond fully in Hindi using Devanagari script only."
        if use_hindi
        else "Respond in English."
    )
    movie_lines: List[str] = []
    for idx, movie in enumerate(movies[:8], start=1):
        title = str(movie.get("title", "")).strip()
        if not title:
            continue
        overview = re.sub(r"\s+", " ", str(movie.get("overview", "")).strip())
        genres = ", ".join(str(g).strip() for g in (movie.get("genres", []) or [])[:3] if str(g).strip())
        director = str(movie.get("director", "")).strip()
        movie_lines.append(
            f'{idx}. {{"title":"{title}","genres":"{genres}","director":"{director}","overview":"{overview[:260]}"}}'
        )

    user_prompt = (
        f"<user_profile>\n{preference_profile}\n</user_profile>\n\n"
        f"<available_recommendations>\n{chr(10).join(movie_lines)}\n</available_recommendations>\n\n"
        f'User query: "{query}"\n\n'
        "Task:\n"
        f"{language_rule}\n"
        "Use only movies from <available_recommendations>.\n"
        "Start with one short opening line.\n"
        "Recommend exactly 3 best movies.\n"
        "For each movie, give only one short description sentence (max 16 words).\n"
        "Speak in a fluid voice-first style. No markdown and no bullets.\n"
        "Use short spoken sentences and natural transitions.\n"
        "End with one short follow-up question.\n"
        "Keep the full reply concise: around 5 spoken sentences total.\n"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


async def generate_grounded_recommendation_text(
    llm: "LLMService",
    query: str,
    movies: List[dict],
    history: List[dict] | None = None,
    output_language: str | None = None,
) -> str:
    if not movies:
        return build_grounded_recommendation_text(query=query, movies=movies, output_language=output_language)

    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    try:
        messages = build_recommendation_messages(
            query=query,
            movies=movies,
            history=history or [],
            output_language=lang,
        )
        text = _trim_spoken_response(await llm.generate_messages(messages), max_sentences=6, max_chars=430)
        if not text:
            return build_grounded_recommendation_text(query=query, movies=movies, output_language=lang)
        if lang == "hi":
            text = await _force_hindi_devanagari(llm, text)
            text = _trim_spoken_response(text, max_sentences=6, max_chars=430)

        movie_titles = [str(m.get("title", "")).strip() for m in movies if str(m.get("title", "")).strip()]
        normalized_answer = _normalize_for_match(text)
        if movie_titles and not any(_normalize_for_match(t) in normalized_answer for t in movie_titles):
            return build_grounded_recommendation_text(query=query, movies=movies, output_language=lang)
        return text
    except Exception:
        return build_grounded_recommendation_text(query=query, movies=movies, output_language=lang)


def build_conversation_messages(
    query: str,
    history: List[dict] | None = None,
    output_language: str | None = None,
) -> List[Dict[str, str]]:
    history = history or []
    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    use_hindi = lang == "hi"
    language_rule = (
        "Respond in Hindi using Devanagari script only."
        if use_hindi
        else "Respond in English."
    )
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        "Conversation mode:\n"
        "- Do not jump into recommendations unless user explicitly asks to suggest/recommend.\n"
        "- Stay movie-first. For unrelated chat, reply briefly and gently pivot back to movies.\n"
        "- Keep response very concise, around 2 to 4 short spoken sentences.\n"
        "- Be warm and conversational.\n"
        "- End with one simple question to continue conversation.\n"
        f"- {language_rule}\n"
    )
    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
    for item in history[-4:]:
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})
    return messages


async def generate_conversation_text(
    llm: "LLMService",
    query: str,
    history: List[dict] | None = None,
    output_language: str | None = None,
) -> str:
    lang = output_language if output_language in {"en", "hi"} else detect_output_language(query)
    try:
        messages = build_conversation_messages(query=query, history=history or [], output_language=lang)
        text = _trim_spoken_response(await llm.generate_messages(messages), max_sentences=4, max_chars=320)
        if lang == "hi":
            text = await _force_hindi_devanagari(llm, text)
            text = _trim_spoken_response(text, max_sentences=4, max_chars=320)
        if text:
            return text
    except Exception:
        pass
    return (
        "मैं सुन रहा हूँ। आप किस तरह की फिल्म का मूड बता रहे हैं?"
        if lang == "hi"
        else "I am listening. What kind of movie mood are you in?"
    )


def get_llm_service(settings: Settings) -> LLMService:
    if settings.use_fine_tuned:
        return FineTunedLLMService(settings)
    return OpenAILLMService(settings)
