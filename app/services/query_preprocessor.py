"""Hybrid Regex+NLU pre-processor.

Runs synchronously (~0.1ms) before the LLM call to extract structured slots
from the user query. Injected into the LLM prompt to reduce hallucination and
allow FAISS post-filtering by decade/language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Slot container
# ---------------------------------------------------------------------------

@dataclass
class QuerySlots:
    years: list[int] = field(default_factory=list)         # explicit years e.g. [2010]
    decade: tuple[int, int] | None = None                  # (1990, 1999)
    rating_min: float | None = None                        # e.g. 8.0
    rating_qualifier: str | None = None                    # "highly_rated" | "recent"
    genres: list[str] = field(default_factory=list)
    language: str | None = None                            # "hindi" | "english"

    def is_empty(self) -> bool:
        return not any([
            self.years, self.decade, self.rating_min,
            self.rating_qualifier, self.genres, self.language,
        ])

    def to_context_string(self) -> str:
        parts: list[str] = []
        if self.decade:
            parts.append(f"Decade: {self.decade[0]}s")
        elif self.years:
            parts.append(f"Year(s): {', '.join(str(y) for y in self.years[:3])}")
        if self.rating_qualifier == "recent":
            parts.append("Recency: latest/recent releases preferred")
        elif self.rating_qualifier == "highly_rated":
            parts.append("Quality: highly rated / top-rated")
        if self.rating_min is not None:
            parts.append(f"Min rating: {self.rating_min}")
        if self.genres:
            parts.append(f"Genre(s): {', '.join(self.genres)}")
        if self.language:
            parts.append(f"Language: {self.language}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_DECADE_MAP = {
    "50s": (1950, 1959), "60s": (1960, 1969), "70s": (1970, 1979),
    "80s": (1980, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
    "2010s": (2010, 2019), "2020s": (2020, 2029),
}
_DECADE_RE = re.compile(
    r"\b(19[0-9]0s|20[0-2]0s|90s|80s|70s|60s|50s)\b", re.IGNORECASE
)
_YEAR_RE = re.compile(r"\b(19[0-9]{2}|20[0-2][0-9])\b")
_RECENT_RE = re.compile(
    r"\b(latest|recent|new|newest|new release|new releases|2024|2025|abhi ki|nayi)\b",
    re.IGNORECASE,
)
_RATING_FLOOR_RE = re.compile(
    r"\b(?:above|over|rated|rating\s+of|minimum\s+rating)\s+([0-9]+(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)
_HIGH_RATING_RE = re.compile(
    r"\b(highly[- ]rated|top[- ]rated|best[- ]rated|highest rated|must[- ]watch|"
    r"critically acclaimed|award winning|award-winning|8\+|9\+)\b",
    re.IGNORECASE,
)
_HINDI_LANG_RE = re.compile(
    r"\b(hindi|bollywood|hindustani|desi|indian film|indian movie)\b|"
    r"[\u0939\u093f\u0928\u094d\u0926\u0940]",  # हिंदी chars
    re.IGNORECASE,
)
_ENGLISH_LANG_RE = re.compile(
    r"\b(english|hollywood|foreign film|foreign movie|international)\b",
    re.IGNORECASE,
)

# Genre keywords (subset — matched case-insensitively)
_GENRES = [
    "action", "thriller", "comedy", "romance", "romantic", "drama",
    "horror", "sci-fi", "sci fi", "science fiction", "adventure",
    "crime", "mystery", "fantasy", "animation", "animated", "family",
    "documentary", "biography", "biographical", "historical", "war",
    "superhero", "heist", "psychological",
]
_GENRE_RE = re.compile(
    r"\b(" + "|".join(re.escape(g) for g in _GENRES) + r")\b",
    re.IGNORECASE,
)

# Devanagari genre tokens
_HINDI_GENRE_MAP = {
    "एक्शन": "action", "थ्रिलर": "thriller", "कॉमेडी": "comedy",
    "रोमांस": "romance", "ड्रामा": "drama", "हॉरर": "horror",
    "साइंस फिक्शन": "sci-fi", "एडवेंचर": "adventure",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_slots(query: str) -> QuerySlots:
    """Extract structured slots from a natural-language movie query."""
    slots = QuerySlots()
    q = query or ""

    # Decade
    dm = _DECADE_RE.search(q)
    if dm:
        key = dm.group(1).lower()
        # Normalise e.g. "1990s" → "90s"
        for canonical, rng in _DECADE_MAP.items():
            if key.endswith(canonical) or key == canonical:
                slots.decade = rng
                break
        if slots.decade is None:
            # fallback: parse the numeric part
            digits = re.search(r"(\d+)", key)
            if digits:
                base = int(digits.group(1))
                if base < 100:
                    base += 1900 if base >= 50 else 2000
                slots.decade = (base, base + 9)

    # Explicit years (only if no decade matched)
    if not slots.decade:
        slots.years = [int(m) for m in _YEAR_RE.findall(q)]

    # Recency
    if _RECENT_RE.search(q):
        slots.rating_qualifier = "recent"

    # High-rating qualifier (only if not already "recent")
    if not slots.rating_qualifier and _HIGH_RATING_RE.search(q):
        slots.rating_qualifier = "highly_rated"

    # Rating floor
    rm = _RATING_FLOOR_RE.search(q)
    if rm:
        try:
            slots.rating_min = float(rm.group(1))
        except ValueError:
            pass

    # Genres (English)
    genres_found = {m.lower() for m in _GENRE_RE.findall(q)}
    # Normalise aliases
    aliases = {"romantic": "romance", "animated": "animation",
               "biographical": "biography", "sci fi": "sci-fi",
               "science fiction": "sci-fi", "psychological": "thriller"}
    slots.genres = [aliases.get(g, g) for g in genres_found]

    # Genres (Hindi/Devanagari)
    for hindi_token, eng_genre in _HINDI_GENRE_MAP.items():
        if hindi_token in q and eng_genre not in slots.genres:
            slots.genres.append(eng_genre)

    # Language
    if _HINDI_LANG_RE.search(q):
        slots.language = "hindi"
    elif _ENGLISH_LANG_RE.search(q):
        slots.language = "english"

    return slots
