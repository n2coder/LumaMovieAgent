from fastapi import APIRouter, HTTPException, Query, Request

from app.models.schemas import RecommendRequest, RecommendResponse
from app.services.llm_service import (
    UNRELATED_REDIRECT_EN,
    UNRELATED_REDIRECT_HI,
    check_identity,
    detect_output_language,
    generate_conversation_text,
    generate_grounded_recommendation_text,
    identity_response,
    is_allowed_query,
    is_recommendation_intent,
    is_small_talk_query,
    policy_response_for_query,
)
from app.services.runtime import AppServices

router = APIRouter(tags=["recommend"])


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(payload: RecommendRequest, request: Request) -> RecommendResponse:
    services: AppServices = request.app.state.services
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    if len(query) > services.settings.max_query_chars:
        raise HTTPException(
            status_code=400,
            detail=f"query too long (max {services.settings.max_query_chars} characters)",
        )

    # Layer 1: deterministic identity override (English + Hindi semantic variants).
    output_language = detect_output_language(query)
    identity_type = check_identity(query)
    if identity_type:
        return RecommendResponse(text=identity_response(identity_type) or "", movies=[], audio_url=None)

    # Layer 2: lightweight allowlist filter to prevent off-domain drift.
    if not is_allowed_query(query):
        refusal = UNRELATED_REDIRECT_HI if output_language == "hi" else UNRELATED_REDIRECT_EN
        return RecommendResponse(text=refusal, movies=[], audio_url=None)

    # Layer 3: policy fallback for any remaining edge conditions.
    policy_text = policy_response_for_query(query)
    if policy_text is not None:
        return RecommendResponse(text=policy_text, movies=[], audio_url=None)

    if is_small_talk_query(query):
        text = await generate_conversation_text(
            llm=services.llm,
            query=query,
            history=[],
            output_language=output_language,
        )
        audio_url = None
        if payload.include_audio and text:
            audio_url = await services.tts.synthesize(text)
        return RecommendResponse(text=text, movies=[], audio_url=audio_url)

    if not is_recommendation_intent(query):
        text = await generate_conversation_text(
            llm=services.llm,
            query=query,
            history=[],
            output_language=output_language,
        )
        audio_url = None
        if payload.include_audio and text:
            audio_url = await services.tts.synthesize(text)
        return RecommendResponse(text=text, movies=[], audio_url=audio_url)

    movies = await services.retriever.retrieve(query, top_k=services.settings.top_k)
    movie_dicts = [movie.as_dict() for movie in movies]
    text = await generate_grounded_recommendation_text(
        llm=services.llm,
        query=query,
        movies=movie_dicts,
        history=[],
        output_language=output_language,
    )

    audio_url = None
    if payload.include_audio and text:
        audio_url = await services.tts.synthesize(text)

    return RecommendResponse(text=text, movies=movie_dicts, audio_url=audio_url)


@router.get("/top-movies")
async def top_movies(
    request: Request,
    limit: int = Query(default=12, ge=1, le=50),
    genre: str = Query(default="", max_length=40),
) -> dict:
    services: AppServices = request.app.state.services
    movies = services.retriever.top_movies(limit=limit, genre=genre)
    return {"movies": [m.as_dict() for m in movies]}


@router.get("/discover-movies")
async def discover_movies(request: Request, limit: int = Query(default=50, ge=1, le=100)) -> dict:
    services: AppServices = request.app.state.services
    movies = services.retriever.top_movies(limit=limit)
    return {"movies": [m.as_dict() for m in movies]}


@router.get("/poster-wall")
async def poster_wall(request: Request, count: int = Query(default=50, ge=1, le=100)) -> dict:
    services: AppServices = request.app.state.services
    posters = services.retriever.random_posters(count=count)
    return {"posters": posters}
