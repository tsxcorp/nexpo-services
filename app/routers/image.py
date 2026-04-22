"""
Image generation router.
POST /image/generate — generates event banners via Nano Banana (Gemini image models).

Security:
- Checks GOOGLE_AI_API_KEY is configured (503 if missing).
- No user data logged; only task, tier, variant count, latency, cost.
- Full base64 response is never logged.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from app.services.image_nano_banana import ImageProviderSafetyError
from app.services.image_prompt_builder import build_prompt
from app.services.image_router import route
from app.services.image_types import ImageGenerateRequest, ImageGenerateResponse
from app.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/image", tags=["image"])


@router.post("/generate", response_model=ImageGenerateResponse)
async def generate_images(req: ImageGenerateRequest) -> ImageGenerateResponse:
    """
    Generate event banner images in parallel for all requested variants.

    - Routes to provider based on task + tier.
    - Builds mood-augmented prompts (optionally injecting brand_kit).
    - Calls provider.generate_batch() with asyncio.gather parallelism.
    - Returns base64 images + aggregate cost + measured latency.
    """
    if not settings.google_ai_api_key:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_AI_API_KEY not configured. Image generation is unavailable.",
        )

    # Route to the correct provider — raises ValueError on unknown task
    try:
        provider = route(req.task, req.tier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Build one prompt per variant
    prompts = [
        build_prompt(
            base_prompt=req.prompt
            + (f" {v.prompt_suffix}" if v.prompt_suffix else ""),
            mood=v.mood,
            brand_kit=req.brand_kit,
        )
        for v in req.variants
    ]
    moods = [v.mood for v in req.variants]

    logger.info(
        "image.generate: task=%s tier=%s variants=%d aspect=%s",
        req.task,
        req.tier,
        len(req.variants),
        req.aspect_ratio,
    )

    t0 = time.perf_counter()
    try:
        images = await provider.generate_batch(
            prompts=prompts,
            aspect_ratio=req.aspect_ratio,
            image_size="2K",
            moods=moods,
            seed=req.seed,
        )
    except ImageProviderSafetyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "content_safety_block",
                "message": str(exc),
                "suggestion": (
                    "Try rephrasing the event description to avoid potentially sensitive content, "
                    "or choose a different mood preset."
                ),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    latency_ms = int((time.perf_counter() - t0) * 1000)
    total_cost = sum(img.cost_usd for img in images)

    logger.info(
        "image.generate: completed variants=%d total_cost_usd=%.4f latency_ms=%d",
        len(images),
        total_cost,
        latency_ms,
    )

    return ImageGenerateResponse(
        images=images,
        total_cost_usd=total_cost,
        latency_ms=latency_ms,
    )
