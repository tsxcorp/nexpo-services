"""
Nano Banana provider: wraps Google Gemini image generation models.
Handles async batching, exponential backoff, and safety error detection.

Model IDs (April 2026):
  - gemini-2.5-flash-image   → $0.04/image, ~1.5s, standard tier
  - gemini-3.1-flash-image-preview → $0.067/image, ~1.5s, premium tier
"""
from __future__ import annotations

import asyncio
import base64
import logging

from app.services.image_types import GeneratedImage
from app.settings import settings

logger = logging.getLogger(__name__)

# Aspect ratio → approximate pixel dimensions at 2K size
_ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "2:1":  (1536, 768),
    "16:9": (1536, 864),
    "9:16": (864, 1536),
}

# Error substrings that indicate rate-limit / resource-exhausted — trigger retry
_RETRYABLE_ERROR_KEYWORDS = ("RATE_LIMIT", "RESOURCE_EXHAUSTED", "429", "quota")

# Retry backoff delays in seconds: attempt 0→0.5s, 1→1s, 2→2s
_BACKOFF_SECONDS = (0.5, 1.0, 2.0)


class ImageProviderSafetyError(Exception):
    """Raised when Gemini returns empty parts (content safety block or generation failure)."""


class NanoBananaProvider:
    """
    Async image generation provider backed by Gemini image models.
    Calls are parallelised via asyncio.gather; each sync SDK call is
    offloaded to a thread via asyncio.to_thread.
    """

    def __init__(
        self,
        model_id: str = "gemini-2.5-flash-image",
        cost_per_image: float = 0.04,
    ) -> None:
        self.model_id = model_id
        self.cost_per_image = cost_per_image

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def generate_batch(
        self,
        prompts: list[str],
        aspect_ratio: str,
        image_size: str = "2K",
        moods: list[str] | None = None,
        seed: int | None = None,
    ) -> list[GeneratedImage]:
        """
        Generate one image per prompt in parallel.

        Args:
            prompts: List of fully-constructed prompt strings (one per variant).
            aspect_ratio: One of "1:1", "2:1", "16:9", "9:16".
            image_size: Gemini image size param ("2K" default).
            moods: Parallel list of mood labels for metadata; defaults to empty strings.
            seed: Optional seed for reproducibility (not officially supported by all models).

        Returns:
            List of GeneratedImage in same order as prompts.

        Raises:
            ImageProviderSafetyError: If any variant is blocked by content safety.
        """
        if moods is None:
            moods = [""] * len(prompts)

        tasks = [
            self._generate_single_with_retry(prompt, aspect_ratio, image_size, mood, seed)
            for prompt, mood in zip(prompts, moods)
        ]
        return await asyncio.gather(*tasks)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _generate_single_with_retry(
        self,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        mood: str,
        seed: int | None,
    ) -> GeneratedImage:
        """Single variant generation with exponential backoff on rate-limit errors."""
        last_error: Exception | None = None

        for attempt, backoff in enumerate(_BACKOFF_SECONDS):
            try:
                return await asyncio.to_thread(
                    self._generate_sync,
                    prompt,
                    aspect_ratio,
                    image_size,
                    mood,
                    seed,
                )
            except ImageProviderSafetyError:
                # Safety block is not retryable — raise immediately
                raise
            except Exception as exc:
                error_str = str(exc)
                is_retryable = any(kw in error_str for kw in _RETRYABLE_ERROR_KEYWORDS)

                if is_retryable and attempt < len(_BACKOFF_SECONDS) - 1:
                    logger.warning(
                        "NanoBanana rate-limit on attempt %d, retrying in %.1fs. "
                        "model=%s prompt_len=%d",
                        attempt + 1,
                        backoff,
                        self.model_id,
                        len(prompt),
                    )
                    await asyncio.sleep(backoff)
                    last_error = exc
                else:
                    raise

        # Should not reach here; raise last captured error
        raise last_error  # type: ignore[misc]

    def _generate_sync(
        self,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        mood: str,
        seed: int | None,
    ) -> GeneratedImage:
        """
        Synchronous Gemini SDK call — run inside asyncio.to_thread.
        Uses genai.Client pattern matching gemini_service.py:56-59.
        """
        from google import genai
        from google.genai import types

        api_key = settings.google_ai_api_key
        if not api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY not configured")

        client = genai.Client(api_key=api_key)

        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        )

        response = client.models.generate_content(
            model=self.model_id,
            contents=[prompt],
            config=config,
        )

        # Safety block returns empty parts — not an exception
        parts = getattr(response, "parts", None) or []
        if not parts:
            # Also check via candidates path
            try:
                parts = response.candidates[0].content.parts
            except (AttributeError, IndexError, TypeError):
                parts = []

        if not parts or not parts[0].inline_data:
            logger.warning(
                "NanoBanana safety block: empty parts. model=%s prompt_len=%d",
                self.model_id,
                len(prompt),
            )
            raise ImageProviderSafetyError(
                f"Image generation blocked by content safety filter for model {self.model_id}. "
                "Try rephrasing the prompt to avoid potentially sensitive content."
            )

        raw_data = parts[0].inline_data.data
        # SDK may return raw bytes or already-base64 encoded string
        if isinstance(raw_data, (bytes, bytearray)):
            image_b64 = base64.b64encode(raw_data).decode("utf-8")
        else:
            # Already base64 string
            image_b64 = raw_data

        mime_type: str = getattr(parts[0].inline_data, "mime_type", "image/png") or "image/png"
        width, height = _ASPECT_DIMENSIONS.get(aspect_ratio, (1024, 1024))

        logger.info(
            "NanoBanana generated image: model=%s aspect=%s mood=%s cost_usd=%.4f",
            self.model_id,
            aspect_ratio,
            mood,
            self.cost_per_image,
        )

        return GeneratedImage(
            base64=image_b64,
            mime_type=mime_type,
            width=width,
            height=height,
            cost_usd=self.cost_per_image,
            provider=self.model_id,
            mood=mood,
            seed=seed,
        )
