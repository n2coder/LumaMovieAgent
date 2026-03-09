from dataclasses import dataclass
from typing import Dict, List

from app.services import vector_retriever


@dataclass
class RetrievedMovie:
    title: str
    overview: str
    genres: List[str]
    top_actors: List[str]
    director: str
    poster_url: str

    def as_dict(self) -> Dict:
        return {
            "title": self.title,
            "overview": self.overview,
            "genres": self.genres,
            "top_actors": self.top_actors,
            "director": self.director,
            "poster_url": self.poster_url,
        }


class Retriever:
    def __init__(self, movies_csv_path: str = "", credits_csv_path: str | None = None, embedding_model_name: str = ""):
        # Interface kept for compatibility; data loading is unified in vector_retriever.
        self._unused = (movies_csv_path, credits_csv_path, embedding_model_name)

    async def retrieve(self, query: str, top_k: int = 5) -> List[RetrievedMovie]:
        rows = await vector_retriever.search(query, top_k=top_k)
        movies: List[RetrievedMovie] = []
        for row in rows:
            movies.append(
                RetrievedMovie(
                    title=str(row.get("title", "")).strip(),
                    overview=str(row.get("overview", "")).strip(),
                    genres=[str(g).strip() for g in (row.get("genres", []) or []) if str(g).strip()],
                    top_actors=[str(a).strip() for a in (row.get("top_actors", []) or []) if str(a).strip()],
                    director=str(row.get("director", "")).strip(),
                    poster_url=str(row.get("poster_url", "")).strip(),
                )
            )
        return movies

    def top_movies(self, limit: int = 20, genre: str = "") -> List[RetrievedMovie]:
        rows = vector_retriever.top_movies(limit=limit, genre=genre)
        return [
            RetrievedMovie(
                title=str(row.get("title", "")).strip(),
                overview=str(row.get("overview", "")).strip(),
                genres=[str(g).strip() for g in (row.get("genres", []) or []) if str(g).strip()],
                top_actors=[str(a).strip() for a in (row.get("top_actors", []) or []) if str(a).strip()],
                director=str(row.get("director", "")).strip(),
                poster_url=str(row.get("poster_url", "")).strip(),
            )
            for row in rows
        ]

    def random_posters(self, count: int = 50) -> List[str]:
        return vector_retriever.random_posters(count=count)
