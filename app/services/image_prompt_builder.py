"""
Mood preset definitions and prompt construction for image generation.
Pure functions — no I/O, no external dependencies.
"""
from __future__ import annotations

# 5 mood presets: each defines style hints injected into the generation prompt
MOOD_PRESETS: dict[str, dict[str, str]] = {
    "professional": {
        "style": "corporate clean layout",
        "palette": "deep navy #06043E and blue #4F80FF, white text",
        "composition": "centered title, subtle geometric lines, premium feel",
        "lighting": "flat light, crisp edges",
    },
    "vibrant": {
        "style": "bold energetic full-bleed gradient",
        "palette": "bright #4F80FF to #2563EB gradient background, white bold text, accent gold",
        "composition": "dynamic diagonal composition, large typography, high contrast",
        "lighting": "vivid saturation, warm highlights",
    },
    "minimal": {
        "style": "minimalist white space, refined simplicity",
        "palette": "white or very light grey background, #4F80FF accent only for key elements",
        "composition": "small centered logo area, generous padding, single accent line",
        "lighting": "bright airy, soft shadows",
    },
    "luxury": {
        "style": "premium high-end editorial",
        "palette": "dark charcoal #1A1A2E or black background, gold #C9A84C accents, white text",
        "composition": "asymmetric layout, large negative space, subtle texture",
        "lighting": "dramatic directional light, deep shadows",
    },
    "tech": {
        "style": "modern technology futuristic",
        "palette": "dark #0D1117 background, electric blue #4F80FF and cyan #22D3EE neon accents",
        "composition": "grid lines, circuit patterns, bold geometric shapes",
        "lighting": "neon glow effects, blue-tinted ambient light",
    },
}


def build_prompt(
    base_prompt: str,
    mood: str,
    brand_kit: dict | None = None,
) -> str:
    """
    Construct a final image generation prompt by combining:
    - base event description
    - mood preset style hints
    - optional brand kit (colors, voice, typography)
    - Vietnamese typography instruction (mandatory for all banners)

    Args:
        base_prompt: Core event description from caller.
        mood: One of MOOD_PRESETS keys.
        brand_kit: Optional dict with keys like primary_color, secondary_color, voice, font_style.

    Returns:
        Complete prompt string for Gemini image generation.
    """
    preset = MOOD_PRESETS.get(mood, MOOD_PRESETS["professional"])

    lines: list[str] = [
        f"Create a high-quality event banner. {base_prompt}",
        "",
        f"Style: {preset['style']}.",
        f"Color palette: {preset['palette']}.",
        f"Composition: {preset['composition']}.",
        f"Lighting: {preset['lighting']}.",
        "",
        # Vietnamese typography is mandatory — banners must render diacritics correctly
        "Typography: Use Vietnamese bold sans-serif font for all text. "
        "Ensure all Vietnamese diacritical marks (accents, tone marks) render cleanly at high resolution. "
        "Font weight 700–800 for headlines, 400–500 for supporting text.",
    ]

    # Inject brand kit if provided
    if brand_kit:
        brand_lines: list[str] = ["", "Brand guidelines (apply these over the mood preset where specified):"]
        if brand_kit.get("primary_color"):
            brand_lines.append(f"  - Primary brand color: {brand_kit['primary_color']}")
        if brand_kit.get("secondary_color"):
            brand_lines.append(f"  - Secondary brand color: {brand_kit['secondary_color']}")
        if brand_kit.get("voice"):
            brand_lines.append(f"  - Brand voice/personality: {brand_kit['voice']}")
        if brand_kit.get("font_style"):
            brand_lines.append(f"  - Typography style: {brand_kit['font_style']}")
        if brand_kit.get("dominant_colors"):
            colors_str = ", ".join(str(c) for c in brand_kit["dominant_colors"])
            brand_lines.append(f"  - Extracted brand colors: {colors_str}")
        lines.extend(brand_lines)

    lines += [
        "",
        "Output: High-resolution event banner suitable for digital display and print. "
        "No borders or frames unless compositionally intentional. "
        "Professional print-ready quality.",
    ]

    return "\n".join(lines)
