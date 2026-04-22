"""
Unit tests for image generation router (POST /image/generate).
All tests mock genai.Client — no real API calls, runs fully offline.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ──────────────────────────────────────────────────────────────────────────────
# App fixture
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    """TestClient with GOOGLE_AI_API_KEY set and genai.Client mocked."""
    monkeypatch.setattr(
        "app.settings.settings.google_ai_api_key",
        "test-api-key",
        raising=False,
    )
    # Import app after patching settings so the router sees the key
    from main import app
    return TestClient(app)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to build fake Gemini responses
# ──────────────────────────────────────────────────────────────────────────────

def _fake_image_bytes() -> bytes:
    """Minimal 1x1 PNG bytes for a deterministic test payload."""
    # Small valid PNG (1×1 white pixel)
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )


def _make_fake_response(raw_bytes: bytes = None) -> MagicMock:
    """Return a MagicMock that mimics a successful Gemini generate_content response."""
    raw = raw_bytes if raw_bytes is not None else _fake_image_bytes()
    inline_data = SimpleNamespace(data=raw, mime_type="image/png")
    part = SimpleNamespace(inline_data=inline_data)
    response = MagicMock()
    response.parts = [part]
    response.candidates = [
        SimpleNamespace(content=SimpleNamespace(parts=[part]))
    ]
    return response


def _make_empty_response() -> MagicMock:
    """Mimics a safety-blocked response with empty parts."""
    response = MagicMock()
    response.parts = []
    response.candidates = []
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: single variant → 1 GeneratedImage returned
# ──────────────────────────────────────────────────────────────────────────────


def test_single_variant_success(client):
    fake_response = _make_fake_response()

    with patch("google.genai.Client") as MockClient:
        MockClient.return_value.models.generate_content.return_value = fake_response

        resp = client.post(
            "/image/generate",
            json={
                "prompt": "Tech fair 2026",
                "task": "event-banner",
                "tier": "standard",
                "aspect_ratio": "16:9",
                "variants": [{"mood": "professional"}],
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["images"]) == 1
    img = data["images"][0]
    assert img["mime_type"] == "image/png"
    assert img["provider"] == "gemini-2.5-flash-image"
    assert img["mood"] == "professional"
    assert img["cost_usd"] == pytest.approx(0.04)
    assert isinstance(img["base64"], str) and len(img["base64"]) > 0
    assert data["total_cost_usd"] == pytest.approx(0.04)
    assert data["latency_ms"] >= 0


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: 4-variant parallel → 4 images, correct total cost
# ──────────────────────────────────────────────────────────────────────────────


def test_four_variant_parallel_success(client):
    fake_response = _make_fake_response()

    with patch("google.genai.Client") as MockClient:
        MockClient.return_value.models.generate_content.return_value = fake_response

        resp = client.post(
            "/image/generate",
            json={
                "prompt": "Career fair 2026",
                "task": "event-banner",
                "tier": "standard",
                "aspect_ratio": "16:9",
                "variants": [
                    {"mood": "professional"},
                    {"mood": "vibrant"},
                    {"mood": "minimal"},
                    {"mood": "tech"},
                ],
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["images"]) == 4
    # Each image costs $0.04 → total $0.16
    assert data["total_cost_usd"] == pytest.approx(0.16, abs=1e-9)
    moods = [img["mood"] for img in data["images"]]
    assert set(moods) == {"professional", "vibrant", "minimal", "tech"}


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: safety block (empty parts) → 422 structured error
# ──────────────────────────────────────────────────────────────────────────────


def test_safety_block_returns_422(client):
    empty_response = _make_empty_response()

    with patch("google.genai.Client") as MockClient:
        MockClient.return_value.models.generate_content.return_value = empty_response

        resp = client.post(
            "/image/generate",
            json={
                "prompt": "Test event",
                "task": "event-banner",
                "tier": "standard",
                "aspect_ratio": "16:9",
                "variants": [{"mood": "professional"}],
            },
        )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "content_safety_block"
    assert "suggestion" in detail


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: rate limit → retry → success
# ──────────────────────────────────────────────────────────────────────────────


def test_rate_limit_retry_then_success(client):
    fake_response = _make_fake_response()

    call_count = {"n": 0}

    def _side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise Exception("RATE_LIMIT exceeded, retry after 30s")
        return fake_response

    with patch("google.genai.Client") as MockClient, patch(
        "app.services.image_nano_banana.asyncio.sleep",
        return_value=None,
    ):
        MockClient.return_value.models.generate_content.side_effect = _side_effect

        resp = client.post(
            "/image/generate",
            json={
                "prompt": "Exhibition expo",
                "task": "event-banner",
                "tier": "standard",
                "aspect_ratio": "16:9",
                "variants": [{"mood": "vibrant"}],
            },
        )

    assert resp.status_code == 200, resp.text
    assert call_count["n"] == 3  # failed twice, succeeded on 3rd
    assert len(resp.json()["images"]) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: prompt builder with brand_kit injection → Vietnamese instruction present
# ──────────────────────────────────────────────────────────────────────────────


def test_prompt_builder_brand_kit_injection():
    from app.services.image_prompt_builder import build_prompt

    brand_kit = {
        "primary_color": "#FF5722",
        "secondary_color": "#1A237E",
        "voice": "energetic and innovative",
    }
    prompt = build_prompt(
        base_prompt="Nexpo Career Fair 2026",
        mood="vibrant",
        brand_kit=brand_kit,
    )

    # Vietnamese typography instruction must always be present
    assert "Vietnamese" in prompt
    assert "sans-serif" in prompt
    # Brand kit values must be injected
    assert "#FF5722" in prompt
    assert "#1A237E" in prompt
    assert "energetic and innovative" in prompt
    # Mood preset must be applied
    assert "gradient" in prompt.lower() or "vibrant" in prompt.lower() or "bold" in prompt.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: invalid task → 422 validation error
# ──────────────────────────────────────────────────────────────────────────────


def test_invalid_task_returns_422(client):
    resp = client.post(
        "/image/generate",
        json={
            "prompt": "Test",
            "task": "vision",  # not in Literal["event-banner", "generic"]
            "tier": "standard",
            "aspect_ratio": "16:9",
            "variants": [{"mood": "professional"}],
        },
    )
    assert resp.status_code == 422
