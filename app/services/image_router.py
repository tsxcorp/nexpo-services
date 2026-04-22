"""
Image provider dispatcher: maps (task, tier) → ImageProvider instance.
Add new providers to PROVIDERS dict without touching router code.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.image_nano_banana import NanoBananaProvider
from app.services.image_types import GeneratedImage


@runtime_checkable
class ImageProvider(Protocol):
    """Protocol all image providers must satisfy."""

    model_id: str
    cost_per_image: float

    async def generate_batch(
        self,
        prompts: list[str],
        aspect_ratio: str,
        image_size: str,
        moods: list[str] | None,
        seed: int | None,
    ) -> list[GeneratedImage]: ...


# Provider registry — add new providers (imagen-4, flux-pro) here only
PROVIDERS: dict[str, NanoBananaProvider] = {
    "nano-banana-flash": NanoBananaProvider(
        model_id="gemini-2.5-flash-image",
        cost_per_image=0.04,
    ),
    "nano-banana-premium": NanoBananaProvider(
        model_id="gemini-3.1-flash-image-preview",
        cost_per_image=0.067,
    ),
}


def route(task: str, tier: str) -> ImageProvider:
    """
    Select a provider based on task type and quality tier.

    Args:
        task: "event-banner" or "generic".
        tier: "fast", "standard", or "premium".

    Returns:
        An ImageProvider instance.

    Raises:
        ValueError: If task is not recognised.
    """
    if task in ("event-banner", "generic"):
        key = "nano-banana-premium" if tier == "premium" else "nano-banana-flash"
        return PROVIDERS[key]

    raise ValueError(
        f"Unknown image task '{task}'. Supported tasks: 'event-banner', 'generic'."
    )
