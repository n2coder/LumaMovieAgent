import ast
import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List

import faiss
import pandas as pd

from app.config import get_settings
from app.services.llm_service import get_llm_service


def _safe_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except (ValueError, SyntaxError):
            pass
        return [value.strip()] if value.strip() else []
    return []


def _normalize_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s\u0900-\u097F]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _contains_devanagari(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def _looks_hinglish(text: str) -> bool:
    q = _normalize_text(text)
    if not q:
        return False
    markers = {
        "mujhe",
        "mujh",
        "chahiye",
        "chahta",
        "chahti",
        "waali",
        "wali",
        "koi",
        "aisa",
        "aisi",
        "jaisa",
        "waise",
        "thoda",
        "achha",
        "accha",
        "batao",
        "dikhao",
        "suggest",
        "hindi",
        "english",
        "movie",
        "film",
    }
    words = q.split()
    hits = sum(1 for w in words if w in markers)
    return hits >= 3


def _detect_language_preference(query: str) -> str | None:
    q = _normalize_text(query)
    hindi_tokens = [
        "hindi movie",
        "hindi film",
        "bollywood",
        "in hindi",
        "????? ?????",
        "????? ????",
        "????? ???",
    ]
    english_tokens = [
        "english movie",
        "english film",
        "hollywood",
        "in english",
        "???????? ?????",
        "????????? ?????",
        "???????? ???",
    ]
    for token in hindi_tokens:
        if _normalize_text(token) in q:
            return "hindi"
    for token in english_tokens:
        if _normalize_text(token) in q:
            return "english"
    return None


class VectorRetriever:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.use_vector_retriever = bool(settings.use_vector_retriever)

        metadata_path = Path(settings.vector_metadata_path)
        if not metadata_path.exists():
            raise FileNotFoundError(f"Metadata file not found: {metadata_path}")

        self.df = pd.read_pickle(metadata_path).fillna("")
        self.llm = get_llm_service(settings)

        self.index = None
        self.model = None

        if self.use_vector_retriever:
            index_path = Path(settings.vector_index_path)
            if index_path.exists():
                try:
                    self.index = faiss.read_index(str(index_path))
                except Exception:
                    self.use_vector_retriever = False
                    self.index = None
            else:
                self.use_vector_retriever = False

    def _ensure_model(self):
        if not self.use_vector_retriever:
            return None
        if self.model is not None:
            return self.model

        try:
            # Lazy import keeps free-tier memory stable until semantic retrieval is explicitly used.
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(self.settings.embedding_model_name or "all-MiniLM-L6-v2")
            return self.model
        except Exception:
            self.use_vector_retriever = False
            self.model = None
            return None

    async def _normalize_query_for_search(self, query: str) -> tuple[str, str | None]:
        raw = (query or "").strip()
        language_pref = _detect_language_preference(raw)
        if not raw:
            return "", language_pref

        # all-MiniLM-L6-v2 is English-focused; normalize Hindi/Hinglish to English for robust retrieval.
        if not _contains_devanagari(raw) and not _looks_hinglish(raw):
            return raw, language_pref

        translate_prompt = (
            "Rewrite this movie search query to concise English for semantic retrieval.\n"
            "Preserve genre/mood/era/theme constraints.\n"
            "Keep language preference only if user explicitly asked for movie language.\n"
            "Return only the rewritten query.\n\n"
            f"Query: {raw}"
        )
        try:
            rewritten = (
                await self.llm.generate_messages(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a retrieval query normalizer. "
                                "Convert user movie queries to concise English search text only."
                            ),
                        },
                        {"role": "user", "content": translate_prompt},
                    ]
                )
            ).strip()
            rewritten = re.sub(r"^['\"`]+|['\"`]+$", "", rewritten).strip()
            if rewritten:
                return rewritten, language_pref
        except Exception:
            pass
        return raw, language_pref

    def _normalize_movie_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": str(record.get("title", "")).strip(),
            "overview": str(record.get("overview", "")).strip(),
            "genres": _safe_list(record.get("genres", [])),
            "top_actors": _safe_list(record.get("top_actors", [])),
            "director": str(record.get("director", "")).strip(),
            "poster_url": str(record.get("poster_url", "")).strip(),
            "popularity": float(record.get("popularity", 0.0) or 0.0),
        }

    def _keyword_candidates(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        if self.df.empty:
            return []

        q = _normalize_text(query)
        tokens = [t for t in q.split() if len(t) > 2]

        if not tokens:
            ranked = self.df.sort_values(by="popularity", ascending=False).head(top_k)
            return [self._normalize_movie_record(r) for r in ranked.to_dict(orient="records")]

        scored: list[tuple[float, float, int]] = []
        for idx, row in enumerate(self.df.itertuples(index=False)):
            title = str(getattr(row, "title", "")).strip()
            overview = str(getattr(row, "overview", "")).strip()
            genres = " ".join(_safe_list(getattr(row, "genres", [])))
            actors = " ".join(_safe_list(getattr(row, "top_actors", [])))
            director = str(getattr(row, "director", "")).strip()
            popularity = float(getattr(row, "popularity", 0.0) or 0.0)

            title_l = title.lower()
            genres_l = genres.lower()
            blob = f"{title} {overview} {genres} {actors} {director}".lower()

            score = 0.0
            for token in tokens:
                if token in title_l:
                    score += 3.0
                elif token in genres_l:
                    score += 2.0
                elif token in blob:
                    score += 1.0

            if score > 0:
                scored.append((score, popularity, idx))

        if not scored:
            ranked = self.df.sort_values(by="popularity", ascending=False).head(top_k)
            return [self._normalize_movie_record(r) for r in ranked.to_dict(orient="records")]

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        picks = [idx for _, _, idx in scored[:top_k]]
        ranked = self.df.iloc[picks]
        return [self._normalize_movie_record(r) for r in ranked.to_dict(orient="records")]

    async def search_movies(self, query: str, top_k: int = 20) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        top_k = max(1, min(int(top_k), len(self.df)))

        if not self.use_vector_retriever or self.index is None:
            return await asyncio.to_thread(self._keyword_candidates, query, top_k)

        def _search_sync() -> List[Dict[str, Any]]:
            model = self._ensure_model()
            if model is None or self.index is None:
                return self._keyword_candidates(query, top_k)
            query_embedding = model.encode([query]).astype("float32")
            _, indices = self.index.search(query_embedding, top_k)
            results = self.df.iloc[indices[0]].to_dict(orient="records")
            return [self._normalize_movie_record(r) for r in results]

        try:
            return await asyncio.to_thread(_search_sync)
        except Exception:
            return await asyncio.to_thread(self._keyword_candidates, query, top_k)

    async def rerank_movies(
        self,
        query: str,
        movies: List[Dict[str, Any]],
        language_preference: str | None = None,
    ) -> str:
        if not movies:
            return ""
        movie_descriptions = "\n".join(
            [f"{i + 1}. {m.get('title', '')} - {str(m.get('overview', ''))[:350]}" for i, m in enumerate(movies)]
        )
        language_rule = (
            f"User explicitly asked for {language_preference} language movies. Prefer those if available."
            if language_preference
            else "User did not explicitly ask for movie language. Rank by semantic relevance only, not language."
        )
        prompt = f"""
User query: {query}

Here are movie candidates:

{movie_descriptions}

{language_rule}

Select the 5 movies that best match the query.
Return only movie titles ranked best to worst.
""".strip()
        return await self.llm.generate_messages(
            [
                {
                    "role": "system",
                    "content": "You are a movie ranking engine. Output only ranked movie titles.",
                },
                {"role": "user", "content": prompt},
            ]
        )

    def _extract_ranked_titles(self, llm_response: str, candidates: List[Dict[str, Any]]) -> List[str]:
        candidate_titles = [str(m.get("title", "")).strip() for m in candidates if str(m.get("title", "")).strip()]
        if not candidate_titles:
            return []

        by_norm: Dict[str, str] = {}
        for title in candidate_titles:
            key = _normalize_title(title)
            if key and key not in by_norm:
                by_norm[key] = title

        ranked: List[str] = []
        seen: set[str] = set()

        chunks: List[str] = []
        for line in (llm_response or "").splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\d+[\)\.:\-]\s*", "", line)
            chunks.extend([p.strip() for p in re.split(r"[,;]", line) if p.strip()])

        for chunk in chunks:
            key = _normalize_title(chunk)
            if key in by_norm:
                title = by_norm[key]
                if title not in seen:
                    ranked.append(title)
                    seen.add(title)
                continue
            for cand_key, cand_title in by_norm.items():
                if (cand_key and cand_key in key) or (key and key in cand_key):
                    if cand_title not in seen:
                        ranked.append(cand_title)
                        seen.add(cand_title)
                    break

        if not ranked:
            full = (llm_response or "").lower()
            positions = []
            for title in candidate_titles:
                pos = full.find(title.lower())
                if pos >= 0:
                    positions.append((pos, title))
            positions.sort(key=lambda x: x[0])
            for _, title in positions:
                if title not in seen:
                    ranked.append(title)
                    seen.add(title)

        for title in candidate_titles:
            if title not in seen:
                ranked.append(title)
                seen.add(title)

        return ranked

    async def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        retrieval_query, language_preference = await self._normalize_query_for_search(query)
        candidates = await self.search_movies(retrieval_query, top_k=max(20, top_k))
        if not candidates:
            return []

        try:
            llm_response = await self.rerank_movies(
                retrieval_query,
                candidates,
                language_preference=language_preference,
            )
            ranked_titles = self._extract_ranked_titles(llm_response, candidates)
        except Exception:
            ranked_titles = [str(m.get("title", "")).strip() for m in candidates]

        by_title: Dict[str, Dict[str, Any]] = {}
        for movie in candidates:
            title = str(movie.get("title", "")).strip()
            if title and title not in by_title:
                by_title[title] = movie

        selected: List[Dict[str, Any]] = []
        for title in ranked_titles:
            movie = by_title.get(title)
            if movie:
                selected.append(movie)
            if len(selected) >= max(1, top_k):
                break
        return selected

    def top_movies(self, limit: int = 20, genre: str = "") -> List[Dict[str, Any]]:
        if self.df.empty:
            return []
        limit = max(1, min(int(limit), len(self.df)))

        df = self.df
        if genre.strip():
            g = genre.strip().lower()

            def _has_genre(value: Any) -> bool:
                return any(str(x).strip().lower() == g for x in _safe_list(value))

            df = df[df["genres"].apply(_has_genre)]

        ranked = df.sort_values(by="popularity", ascending=False).head(limit)
        return [self._normalize_movie_record(r) for r in ranked.to_dict(orient="records")]

    def random_posters(self, count: int = 50) -> List[str]:
        if self.df.empty:
            return []
        count = max(1, int(count))
        posters = self.df[self.df["poster_url"].astype(str).str.len() > 0]["poster_url"]
        if posters.empty:
            return []
        sample_size = min(count, len(posters))
        return posters.sample(n=sample_size, replace=False).astype(str).tolist()


_retriever: VectorRetriever | None = None


def _get_retriever() -> VectorRetriever:
    global _retriever
    if _retriever is None:
        _retriever = VectorRetriever()
    return _retriever


async def search(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    return await _get_retriever().search(query=query, top_k=top_k)


async def search_movies(query: str, top_k: int = 20) -> List[Dict[str, Any]]:
    return await _get_retriever().search_movies(query=query, top_k=top_k)


async def rerank_movies(query: str, movies: List[Dict[str, Any]]) -> str:
    return await _get_retriever().rerank_movies(query=query, movies=movies)


def top_movies(limit: int = 20, genre: str = "") -> List[Dict[str, Any]]:
    return _get_retriever().top_movies(limit=limit, genre=genre)


def random_posters(count: int = 50) -> List[str]:
    return _get_retriever().random_posters(count=count)
