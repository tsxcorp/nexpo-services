"""AI and keyword-based matching logic."""
import json
import logging
import re
import httpx
from typing import List
from app.config import OPENROUTER_API_KEY, NOVITA_API_KEY, OPENAI_API_KEY, ai_semaphore

logger = logging.getLogger(__name__)


_TOKEN_SPLIT = re.compile(r"[^\w\u00C0-\u1EF9]+", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    """Tokenize free-form text + JSON answer values into clean word set.

    Handles: JSON arrays like `["Foo Bar"]`, punctuation, brackets, quotes.
    Keeps Vietnamese diacritic chars via Unicode range.
    """
    if not text:
        return set()
    words = _TOKEN_SPLIT.split(text.lower())
    return {w for w in words if len(w) > 1}


# LLM scoring provider chain: Novita (deepseek) → OpenAI → OpenRouter → keyword fallback.
# Novita first: cheap + fast. OpenAI second: reliable. OpenRouter last: aggregated fallback.
_SCORING_PROVIDERS: list[dict] = []
if NOVITA_API_KEY:
    _SCORING_PROVIDERS.append({
        "name": "Novita (deepseek-v3)",
        "url": "https://api.novita.ai/v3/openai/chat/completions",
        "key": NOVITA_API_KEY,
        "model": "deepseek/deepseek-v3-0324",
    })
if OPENAI_API_KEY:
    _SCORING_PROVIDERS.append({
        "name": "OpenAI (gpt-4o-mini)",
        "url": "https://api.openai.com/v1/chat/completions",
        "key": OPENAI_API_KEY,
        "model": "gpt-4o-mini",
    })
if OPENROUTER_API_KEY:
    _SCORING_PROVIDERS.append({
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key": OPENROUTER_API_KEY,
        "model": None,  # filled per-call from request.ai_model
    })


async def _call_llm_for_scoring(prompt: str, openrouter_model: str) -> dict | None:
    """Run prompt through provider chain. Returns parsed JSON dict on success, None if all fail.

    Strips markdown code fences, handles <think> tags (minimax).
    """
    last_err: Exception | None = None
    for prov in _SCORING_PROVIDERS:
        model = prov["model"] or openrouter_model
        try:
            async with ai_semaphore:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        prov["url"],
                        headers={
                            "Authorization": f"Bearer {prov['key']}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.1,
                            "max_tokens": 512,
                        },
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    text = result["choices"][0]["message"]["content"].strip()
                    # Strip <think>...</think> reasoning blocks (minimax, deepseek-r1)
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                    # Strip markdown code fences
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    return json.loads(text.strip())
        except httpx.HTTPStatusError as e:
            logger.warning(
                "%s HTTP %s — trying next provider. Body: %s",
                prov["name"], e.response.status_code, e.response.text[:200],
            )
            last_err = e
            continue
        except Exception as e:
            logger.warning("%s failed: %s — trying next provider", prov["name"], e)
            last_err = e
            continue
    if last_err:
        logger.error("All scoring providers failed; last error: %s", last_err)
    return None


async def score_match_with_gemini(job: dict, visitor_profile: dict, model: str = "openai/gpt-4o-mini") -> dict:
    """Use OpenRouter to score how well a visitor matches a job requirement."""
    if not OPENROUTER_API_KEY:
        return _simple_score_match(job, visitor_profile)

    prompt = f"""You are a hiring assistant. Score how well this job seeker matches the job requirement.

JOB REQUIREMENT:
- Title: {job.get('job_title', 'N/A')}
- Description: {job.get('description', 'N/A')}
- Requirements: {job.get('requirements', 'N/A')}
- Skills needed: {json.dumps(job.get('skills', []))}
- Experience level: {job.get('experience_level', 'N/A')}
- Employment type: {job.get('employment_type', 'N/A')}

JOB SEEKER PROFILE:
{json.dumps(visitor_profile, ensure_ascii=False, indent=2)}

Respond ONLY with valid JSON in this exact format:
{{
  "score": <float 0.0-1.0>,
  "matched_criteria": {{
    "skills_match": <float 0.0-1.0>,
    "experience_match": <float 0.0-1.0>,
    "role_match": <float 0.0-1.0>
  }},
  "reasoning": "<1-2 sentence explanation>"
}}"""

    try:
        async with ai_semaphore:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 512,
                    },
                )
                resp.raise_for_status()
                result = resp.json()
                text = result["choices"][0]["message"]["content"].strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                parsed = json.loads(text.strip())
                return {
                    "score": float(parsed.get("score", 0.5)),
                    "matched_criteria": parsed.get("matched_criteria", {}),
                    "ai_reasoning": parsed.get("reasoning", ""),
                }
    except Exception:
        return _simple_score_match(job, visitor_profile)


def _simple_score_match(job: dict, visitor_profile: dict) -> dict:
    """Fallback: keyword-based matching when OpenRouter unavailable."""
    job_text = " ".join([
        str(job.get("job_title", "")),
        str(job.get("description", "")),
        str(job.get("requirements", "")),
        " ".join(job.get("skills", []) or []),
    ]).lower()

    profile_text = json.dumps(visitor_profile, ensure_ascii=False).lower()

    job_words = set(job_text.split())
    profile_words = set(profile_text.split())
    stopwords = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "is", "are", "với", "và", "của"}
    job_words -= stopwords
    profile_words -= stopwords

    if not job_words:
        score = 0.5
    else:
        overlap = len(job_words & profile_words)
        score = min(overlap / max(len(job_words), 1) * 2, 1.0)

    return {
        "score": round(score, 2),
        "matched_criteria": {"keyword_overlap": round(score, 2)},
        "ai_reasoning": f"Keyword-based score: {round(score * 100)}% overlap (OpenRouter key not configured)",
    }


def keyword_prefilter_score(job: dict, profile: dict) -> float:
    """Fast keyword overlap check before calling AI. Returns 0.0-1.0."""
    job_text = " ".join(filter(None, [
        str(job.get("job_title") or ""),
        str(job.get("description") or ""),
        str(job.get("skills") or ""),
        str(job.get("requirements") or ""),
        str(job.get("employment_type") or ""),
        str(job.get("experience_level") or ""),
    ])).lower()
    candidate_text = " ".join(str(v) for v in profile.values() if v).lower()
    if not job_text or not candidate_text:
        return 0.0
    job_words = set(job_text.split())
    candidate_words = set(candidate_text.split())
    if not job_words:
        return 0.0
    overlap = job_words & candidate_words
    return len(overlap) / len(job_words)


async def score_business_match(requirement: dict, visitor_profile: dict, model: str = "openai/gpt-4o-mini") -> dict:
    """Use OpenRouter to score how well a visitor/exhibitor matches a business requirement."""
    if not OPENROUTER_API_KEY:
        return _simple_business_score(requirement, visitor_profile)

    prompt = f"""You are a business partnership analyst. Score how well this visitor/company matches the business requirement.

BUSINESS REQUIREMENT:
- Type: {requirement.get('requirement_type', 'N/A')}
- Target Markets: {json.dumps(requirement.get('target_markets', []))}
- Industry Focus: {json.dumps(requirement.get('industry_focus', []))}
- Company Size Preference: {json.dumps(requirement.get('company_size_preference', []))}
- Partnership Goals: {requirement.get('partnership_goals', 'N/A')}
- Must-Have Criteria: {json.dumps(requirement.get('must_have_criteria', 'N/A'))}
- Nice-to-Have Criteria: {json.dumps(requirement.get('nice_to_have_criteria', 'N/A'))}
- Summary: {requirement.get('summary', 'N/A')}

VISITOR/COMPANY PROFILE:
{json.dumps(visitor_profile, ensure_ascii=False, indent=2)}

Respond ONLY with valid JSON in this exact format:
{{
  "score": <float 0.0-1.0>,
  "matched_criteria": {{
    "market_match": <float 0.0-1.0>,
    "industry_match": <float 0.0-1.0>,
    "requirement_match": <float 0.0-1.0>
  }},
  "reasoning": "<1-2 sentence explanation>"
}}"""

    try:
        async with ai_semaphore:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 512,
                    },
                )
                resp.raise_for_status()
                result = resp.json()
                text = result["choices"][0]["message"]["content"].strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                parsed = json.loads(text.strip())
                return {
                    "score": float(parsed.get("score", 0.5)),
                    "matched_criteria": parsed.get("matched_criteria", {}),
                    "ai_reasoning": parsed.get("reasoning", ""),
                }
    except Exception:
        return _simple_business_score(requirement, visitor_profile)


def _simple_business_score(requirement: dict, visitor_profile: dict) -> dict:
    """Fallback: keyword-based matching when OpenRouter unavailable."""
    req_text = " ".join(filter(None, [
        str(requirement.get("requirement_type", "")),
        str(requirement.get("partnership_goals", "")),
        str(requirement.get("summary", "")),
        " ".join(requirement.get("target_markets", []) or []),
        " ".join(requirement.get("industry_focus", []) or []),
        json.dumps(requirement.get("must_have_criteria", "")),
    ])).lower()

    profile_text = json.dumps(visitor_profile, ensure_ascii=False).lower()

    req_words = set(req_text.split())
    profile_words = set(profile_text.split())
    stopwords = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "is", "are", "với", "và", "của"}
    req_words -= stopwords
    profile_words -= stopwords

    if not req_words:
        score = 0.5
    else:
        overlap = len(req_words & profile_words)
        score = min(overlap / max(len(req_words), 1) * 2, 1.0)

    return {
        "score": round(score, 2),
        "matched_criteria": {"keyword_overlap": round(score, 2)},
        "ai_reasoning": f"Keyword-based score: {round(score * 100)}% overlap (OpenRouter key not configured)",
    }


def business_keyword_prefilter_score(requirement: dict, profile: dict) -> float:
    """Fast keyword overlap for business requirements before calling AI. Returns 0.0-1.0."""
    req_text = " ".join(filter(None, [
        str(requirement.get("requirement_type") or ""),
        str(requirement.get("partnership_goals") or ""),
        str(requirement.get("summary") or ""),
        " ".join(requirement.get("target_markets", []) or []),
        " ".join(requirement.get("industry_focus", []) or []),
        json.dumps(requirement.get("must_have_criteria") or ""),
        json.dumps(requirement.get("nice_to_have_criteria") or ""),
    ])).lower()
    candidate_text = " ".join(str(v) for v in profile.values() if v).lower()
    if not req_text or not candidate_text:
        return 0.0
    req_words = set(req_text.split())
    candidate_words = set(candidate_text.split())
    if not req_words:
        return 0.0
    overlap = req_words & candidate_words
    return len(overlap) / len(req_words)


async def score_profile_business_match(exhibitor_profile: dict, visitor_profile: dict, model: str = "openai/gpt-4o-mini") -> dict:
    """Score how well a visitor matches an exhibitor's profile and matching goals.

    Provider chain: Novita (deepseek-v3) → OpenRouter → keyword fallback.
    """
    if not _SCORING_PROVIDERS:
        return _simple_profile_business_score(exhibitor_profile, visitor_profile)

    prompt = f"""You are a B2B matchmaker at a trade exhibition. Score how well this visitor matches the exhibitor's company profile and matching goals.

EXHIBITOR PROFILE:
- Company: {exhibitor_profile.get('company_name', 'N/A')}
- Description: {exhibitor_profile.get('company_description', 'N/A')}
- Industry: {exhibitor_profile.get('industry', 'N/A')}
- Additional Industries: {json.dumps(exhibitor_profile.get('industries', []))}
- Country: {exhibitor_profile.get('country', 'N/A')}
- B2B Matching Goals: {exhibitor_profile.get('matching_goals', 'N/A')}

VISITOR PROFILE:
{json.dumps(visitor_profile, ensure_ascii=False, indent=2)}

Respond ONLY with valid JSON in this exact format:
{{
  "score": <float 0.0-1.0>,
  "matched_criteria": {{
    "goals_match": <float 0.0-1.0>,
    "industry_match": <float 0.0-1.0>,
    "profile_match": <float 0.0-1.0>
  }},
  "reasoning": "<1-2 sentence explanation>"
}}"""

    parsed = await _call_llm_for_scoring(prompt, openrouter_model=model)
    if parsed is None:
        return _simple_profile_business_score(exhibitor_profile, visitor_profile)
    return {
        "score": float(parsed.get("score", 0.5)),
        "matched_criteria": parsed.get("matched_criteria", {}),
        "ai_reasoning": parsed.get("reasoning", ""),
    }


def _simple_profile_business_score(exhibitor_profile: dict, visitor_profile: dict) -> dict:
    """Fallback: keyword-based scoring for profile matching."""
    ex_text = " ".join(filter(None, [
        str(exhibitor_profile.get("matching_goals", "")),
        str(exhibitor_profile.get("company_description", "")),
        str(exhibitor_profile.get("industry", "")),
        " ".join(exhibitor_profile.get("industries", []) or []),
    ]))
    profile_text = json.dumps(visitor_profile, ensure_ascii=False)

    stopwords = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "is", "are", "với", "và", "của"}
    ex_words = _tokenize(ex_text) - stopwords
    profile_words = _tokenize(profile_text) - stopwords

    if not ex_words:
        score = 0.5
    else:
        overlap = len(ex_words & profile_words)
        score = min(overlap / max(len(ex_words), 1) * 2, 1.0)

    return {
        "score": round(score, 2),
        "matched_criteria": {"keyword_overlap": round(score, 2)},
        "ai_reasoning": f"Keyword-based score: {round(score * 100)}% overlap (OpenRouter key not configured)",
    }


def profile_keyword_prefilter_score(exhibitor_profile: dict, visitor_profile: dict) -> float:
    """Fast keyword overlap for exhibitor profile vs visitor profile. Returns 0.0-1.0.

    Note: returns 0 when languages mismatch (VN goals vs EN answers) — caller should
    default `keyword_threshold=0` so AI scoring still runs on semantic similarity.
    """
    ex_text = " ".join(filter(None, [
        str(exhibitor_profile.get("matching_goals") or ""),
        str(exhibitor_profile.get("company_description") or ""),
        str(exhibitor_profile.get("industry") or ""),
        " ".join(exhibitor_profile.get("industries", []) or []),
    ]))
    candidate_text = " ".join(str(v) for v in visitor_profile.values() if v)
    ex_words = _tokenize(ex_text)
    candidate_words = _tokenize(candidate_text)
    if not ex_words or not candidate_words:
        return 0.0
    overlap = ex_words & candidate_words
    return len(overlap) / len(ex_words)


async def extract_visitor_profile(submission: dict, matching_fields: List[dict]) -> dict:
    """Extract visitor profile from form submission answers."""
    profile = {}
    answers = submission.get("answers", []) or []
    for answer in answers:
        field_id = None
        if isinstance(answer.get("field"), dict):
            field_id = answer["field"].get("id")
        elif isinstance(answer.get("field"), str):
            field_id = answer["field"]

        matching_field = next((f for f in matching_fields if str(f.get("id")) == str(field_id)), None)
        if matching_field and matching_field.get("use_for_matching"):
            attr = matching_field.get("matching_attribute", "other")
            label = None
            for t in (matching_field.get("translations") or []):
                if t.get("languages_code") in ("en-US", "vi-VN"):
                    label = t.get("label")
                    break
            key = attr if attr else (label or field_id or "field")
            profile[key] = answer.get("value")
    return profile
