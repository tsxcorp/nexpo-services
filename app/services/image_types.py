"""
Pydantic v2 models for image generation service.
Used by image.py router + NanoBananaProvider.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BannerVariant(BaseModel):
    """A single image variant request with mood + optional prompt suffix."""

    mood: Literal["professional", "vibrant", "minimal", "luxury", "tech"]
    prompt_suffix: str | None = None


class ImageGenerateRequest(BaseModel):
    """Request schema for POST /image/generate."""

    prompt: str = Field(..., min_length=1, max_length=2000, description="Core event description")
    task: Literal["event-banner", "generic"]
    tier: Literal["fast", "standard", "premium"] = "standard"
    aspect_ratio: Literal["1:1", "2:1", "16:9", "9:16"] = "16:9"
    variants: list[BannerVariant] = Field(..., min_length=1, max_length=8)
    brand_kit: dict | None = None  # colors, fonts, voice injected by caller
    seed: int | None = None


class GeneratedImage(BaseModel):
    """A single generated image result."""

    base64: str
    mime_type: str = "image/png"
    width: int
    height: int
    cost_usd: float
    provider: str
    mood: str
    seed: int | None = None


class ImageGenerateResponse(BaseModel):
    """Response schema for POST /image/generate."""

    images: list[GeneratedImage]
    total_cost_usd: float
    latency_ms: int
