"""Business matching AI endpoints — requirement-based and profile-based modes."""
import asyncio
import logging
from typing import List
from fastapi import APIRouter, HTTPException
import httpx

logger = logging.getLogger(__name__)
from app.models.schemas import (
    BusinessMatchRunRequest,
    BusinessMatchRunResponse,
    BusinessMatchSuggestionOut,
    ProfileMatchRunRequest,
    ProfileMatchRunResponse,
    ProfileMatchSuggestionOut,
)
from app.config import DIRECTUS_ADMIN_TOKEN, ADMIN_URL
from app.services.directus import directus_get, directus_post, directus_patch, create_notification
from app.services.matching_service import (
    score_business_match,
    business_keyword_prefilter_score,
    extract_visitor_profile,
    score_profile_business_match,
    profile_keyword_prefilter_score,
)

router = APIRouter()


@router.post("/match/business/run", response_model=BusinessMatchRunResponse)
async def run_business_matching(request: BusinessMatchRunRequest):
    """
    Run AI business matching for an event.
    Fetches business_requirements + visitor submissions, scores with AI,
    and creates/updates business_match_suggestions in Directus.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTUS_ADMIN_TOKEN not configured")

    event_id = request.event_id

    try:
        # 1. Fetch business requirements
        if request.business_requirement_id:
            req_filter = f"filter[id][_eq]={request.business_requirement_id}"
        elif request.exhibitor_id:
            req_filter = (
                f"filter[event_id][_eq]={event_id}"
                f"&filter[status][_eq]=published"
                f"&filter[exhibitor_id][_eq]={request.exhibitor_id}"
            )
        else:
            req_filter = f"filter[event_id][_eq]={event_id}&filter[status][_eq]=published"

        reqs_resp = await directus_get(
            f"/items/business_requirements?{req_filter}"
            "&fields[]=id,requirement_type,target_markets,industry_focus,"
            "company_size_preference,partnership_goals,must_have_criteria,"
            "nice_to_have_criteria,summary,exhibitor_id"
            "&limit=100"
        )
        requirements = reqs_resp.get("data", [])
        if not requirements:
            return BusinessMatchRunResponse(
                success=True,
                message="No published business requirements found",
                suggestions_created=0,
            )

        # 2. Form fields for matching
        fields_resp = await directus_get(
            f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
            "&fields[]=id,name,use_for_matching,matching_attribute,"
            "translations.languages_code,translations.label"
            "&limit=200"
        )
        matching_fields = fields_resp.get("data", [])

        # 3. Visitor registrations with submissions
        regs_resp = await directus_get(
            f"/items/registrations?filter[event_id][_eq]={event_id}"
            "&filter[submissions][_nnull]=true"
            "&fields[]=id,submissions.id,submissions.form,"
            "submissions.answers.value,submissions.answers.field.id"
            "&limit=500"
        )
        registrations = regs_resp.get("data", [])

        form_ids_resp = await directus_get(
            f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
            "&fields[]=form_id&limit=50"
        )
        form_ids = {
            item.get("form_id")
            for item in form_ids_resp.get("data", [])
            if item.get("form_id")
        }

        # Build visitor profiles from registration submissions
        submissions = []
        for reg in registrations:
            sub = reg.get("submissions")
            if not sub or not isinstance(sub, dict):
                continue
            sub_form = sub.get("form")
            if form_ids and sub_form not in form_ids:
                continue
            profile = await extract_visitor_profile(
                {"answers": sub.get("answers") or []}, matching_fields
            )
            if profile:
                submissions.append({
                    "registration_id": reg["id"],
                    "answers": [],
                    "_merged_profile": profile,
                })

        if not submissions:
            return BusinessMatchRunResponse(
                success=True,
                message="No visitor profiles found for matching",
                suggestions_created=0,
            )

        # Pre-load existing suggestions to avoid N+1 queries
        existing_resp = await directus_get(
            f"/items/business_match_suggestions?filter[event_id][_eq]={event_id}"
            "&fields[]=id,business_requirement_id,registration_id,status&limit=2000"
        )
        existing_map: dict = {}
        for s in existing_resp.get("data", []):
            key = (
                str(s.get("business_requirement_id", "")),
                str(s.get("registration_id", "")),
            )
            existing_map[key] = {"id": s["id"], "status": s.get("status", "pending")}

        suggestions_created = 0
        suggestions_updated = 0
        all_suggestions: List[BusinessMatchSuggestionOut] = []
        suggestions_by_exhibitor: dict[str, int] = {}
        SCORE_THRESHOLD = max(0.1, min(0.95, request.score_threshold))
        KEYWORD_THRESHOLD = max(0.0, min(0.5, request.keyword_threshold))
        MAX_CANDIDATES = max(5, min(200, request.max_candidates_per_requirement))

        for req in requirements:
            exhibitor_id = req.get("exhibitor_id")

            # Keyword prefilter
            scored_submissions = []
            for submission in submissions:
                registration_id = submission.get("registration_id")
                if not registration_id:
                    continue
                visitor_profile = submission.get("_merged_profile") or {}
                if not visitor_profile:
                    continue
                kw_score = business_keyword_prefilter_score(req, visitor_profile)
                if kw_score >= KEYWORD_THRESHOLD:
                    scored_submissions.append((kw_score, submission, visitor_profile))

            scored_submissions.sort(key=lambda x: x[0], reverse=True)
            top_submissions = scored_submissions[:MAX_CANDIDATES]

            # AI scoring — parallel via asyncio.gather (semaphore gates concurrency)
            async def _score_one(sub: dict, vp: dict):
                return sub, await score_business_match(req, vp, model=request.ai_model)

            score_tasks = [_score_one(s, vp) for _, s, vp in top_submissions]
            score_results = await asyncio.gather(*score_tasks, return_exceptions=True)

            for result in score_results:
                if isinstance(result, Exception):
                    continue
                submission, score_result = result
                registration_id = submission.get("registration_id")
                score = score_result["score"]
                if score < SCORE_THRESHOLD:
                    continue

                reg_id_str = str(registration_id)
                suggestion = BusinessMatchSuggestionOut(
                    business_requirement_id=str(req["id"]),
                    registration_id=reg_id_str,
                    exhibitor_id=str(exhibitor_id) if exhibitor_id else "",
                    score=score,
                    matched_criteria=score_result["matched_criteria"],
                    ai_reasoning=score_result["ai_reasoning"],
                )
                all_suggestions.append(suggestion)

                key = (suggestion.business_requirement_id, suggestion.registration_id)
                existing = existing_map.get(key)
                suggestion_data = {
                    "event_id": event_id,
                    "business_requirement_id": suggestion.business_requirement_id,
                    "registration_id": suggestion.registration_id,
                    "exhibitor_id": suggestion.exhibitor_id or None,
                    "score": round(score, 4),
                    "matched_criteria": suggestion.matched_criteria,
                    "ai_reasoning": suggestion.ai_reasoning,
                    "source": "ai_matching",
                }

                if existing:
                    if existing["status"] not in ("pending",):
                        continue
                    if not request.rescore_pending:
                        continue
                    await directus_patch(
                        f"/items/business_match_suggestions/{existing['id']}",
                        suggestion_data,
                    )
                    suggestions_updated += 1
                else:
                    await directus_post(
                        "/items/business_match_suggestions",
                        {**suggestion_data, "status": "pending"},
                    )
                    suggestions_created += 1
                    ex_id = str(exhibitor_id) if exhibitor_id else ""
                    if ex_id:
                        suggestions_by_exhibitor[ex_id] = (
                            suggestions_by_exhibitor.get(ex_id, 0) + 1
                        )

        # In-app notification to organizer
        if suggestions_by_exhibitor:
            try:
                event_resp = await directus_get(
                    f"/items/events/{event_id}?fields[]=user_created"
                )
                organizer_user_id = (event_resp.get("data") or {}).get("user_created")
                if organizer_user_id:
                    total_new = sum(suggestions_by_exhibitor.values())
                    await create_notification(
                        user_id=organizer_user_id,
                        title=f"{total_new} gợi ý business matching mới từ AI",
                        body=f"{len(suggestions_by_exhibitor)} exhibitor(s) có đối tác tiềm năng mới",
                        link=f"{ADMIN_URL}/events/{event_id}/business-matching/ai",
                        notif_type="matching_complete",
                    )
            except Exception:
                pass

        total_candidates = len(submissions)
        return BusinessMatchRunResponse(
            success=True,
            message=(
                f"Matching complete. {suggestions_created} new, {suggestions_updated} refreshed. "
                f"Checked {len(requirements)} requirement(s) × top-{MAX_CANDIDATES} of "
                f"{total_candidates} candidates "
                f"(min score {int(SCORE_THRESHOLD*100)}%, keyword "
                f"{int(KEYWORD_THRESHOLD*100)}%, model {request.ai_model})."
            ),
            suggestions_created=suggestions_created,
            suggestions=all_suggestions,
        )

    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Directus error: {e.response.status_code} {e.response.text[:200]}",
        )
    except Exception as e:
        logger.exception("Business matching error")
        raise HTTPException(
            status_code=500, detail="Business matching failed"
        )


# ---------------------------------------------------------------------------
# Helper: fetch visitor profiles for an event (shared by both modes)
# ---------------------------------------------------------------------------

async def _fetch_visitor_profiles(event_id: int) -> list[dict]:
    """Fetch registrations with form submissions and extract matching profiles."""
    fields_resp = await directus_get(
        f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
        "&fields[]=id,name,use_for_matching,matching_attribute,"
        "translations.languages_code,translations.label"
        "&limit=200"
    )
    matching_fields = fields_resp.get("data", [])

    regs_resp = await directus_get(
        f"/items/registrations?filter[event_id][_eq]={event_id}"
        "&filter[submissions][_nnull]=true"
        "&fields[]=id,submissions.id,submissions.form,"
        "submissions.answers.value,submissions.answers.field.id"
        "&limit=500"
    )
    registrations = regs_resp.get("data", [])

    form_ids_resp = await directus_get(
        f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
        "&fields[]=form_id&limit=50"
    )
    form_ids = {
        item.get("form_id")
        for item in form_ids_resp.get("data", [])
        if item.get("form_id")
    }

    profiles = []
    for reg in registrations:
        sub = reg.get("submissions")
        if not sub or not isinstance(sub, dict):
            continue
        sub_form = sub.get("form")
        if form_ids and sub_form not in form_ids:
            continue
        profile = await extract_visitor_profile(
            {"answers": sub.get("answers") or []}, matching_fields
        )
        if profile:
            profiles.append({
                "registration_id": reg["id"],
                "_merged_profile": profile,
            })
    return profiles


# ---------------------------------------------------------------------------
# Profile-based business matching endpoint
# ---------------------------------------------------------------------------

@router.post("/match/business/profile-run", response_model=ProfileMatchRunResponse)
async def run_profile_business_matching(request: ProfileMatchRunRequest):
    """
    Run AI business matching using exhibitor profiles (matching_goals, industry, etc.)
    instead of manually created business_requirements.
    Results stored in business_match_suggestions with source='profile_matching'.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTUS_ADMIN_TOKEN not configured")

    event_id = request.event_id

    try:
        # 1. Fetch exhibitor_events with exhibitor profile data
        ee_filter = f"filter[event_id][_eq]={event_id}"
        if request.exhibitor_id:
            ee_filter += f"&filter[exhibitor_id][_eq]={request.exhibitor_id}"

        ee_resp = await directus_get(
            f"/items/exhibitor_events?{ee_filter}"
            "&fields[]=exhibitor_id.id,exhibitor_id.industry_id.translations.languages_code,"
            "exhibitor_id.industry_id.translations.category,"
            "exhibitor_id.industries,exhibitor_id.country.name,"
            "exhibitor_id.translations.languages_code,"
            "exhibitor_id.translations.company_name,"
            "exhibitor_id.translations.company_description,"
            "matching_goals"
            "&limit=200"
        )
        exhibitor_events = ee_resp.get("data", [])

        # Build exhibitor profiles — only include those with matching_goals
        exhibitor_profiles: list[dict] = []
        for ee in exhibitor_events:
            matching_goals = ee.get("matching_goals")
            if not matching_goals:
                continue  # skip exhibitors without matching goals

            ex = ee.get("exhibitor_id") or {}
            if isinstance(ex, str):
                continue  # relation not expanded
            ex_id = ex.get("id")
            if not ex_id:
                continue

            # Extract industry label
            industry = ""
            ind = ex.get("industry_id")
            if isinstance(ind, dict):
                for tr in (ind.get("translations") or []):
                    if tr.get("languages_code") in ("vi-VN", "en-US"):
                        industry = tr.get("category", "")
                        break

            # Extract company name + description from translations
            company_name = ""
            company_description = ""
            for tr in (ex.get("translations") or []):
                lang = tr.get("languages_code", "")
                if lang in ("vi-VN", "en-US"):
                    company_name = company_name or tr.get("company_name", "")
                    company_description = company_description or tr.get("company_description", "")

            country_name = ""
            country = ex.get("country")
            if isinstance(country, dict):
                country_name = country.get("name", "")

            exhibitor_profiles.append({
                "exhibitor_id": ex_id,
                "company_name": company_name,
                "company_description": company_description,
                "industry": industry,
                "industries": ex.get("industries") or [],
                "country": country_name,
                "matching_goals": matching_goals,
            })

        if not exhibitor_profiles:
            if request.exhibitor_id:
                msg = (
                    "Exhibitor has no B2B matching goals set. "
                    "Fill 'Matching goals' on the exhibitor profile before running profile matching."
                )
            else:
                msg = (
                    "No exhibitors have B2B matching goals set for this event. "
                    "Fill 'Matching goals' on at least one exhibitor first."
                )
            return ProfileMatchRunResponse(
                success=True,
                message=msg,
                suggestions_created=0,
            )

        # 2. Fetch visitor profiles
        visitor_submissions = await _fetch_visitor_profiles(event_id)
        if not visitor_submissions:
            return ProfileMatchRunResponse(
                success=True,
                message="No visitor profiles found for matching",
                suggestions_created=0,
            )

        # 3. Pre-load existing suggestions
        existing_resp = await directus_get(
            f"/items/business_match_suggestions?filter[event_id][_eq]={event_id}"
            "&filter[source][_eq]=profile_matching"
            "&fields[]=id,exhibitor_id,registration_id,status&limit=2000"
        )
        existing_map: dict = {}
        for s in existing_resp.get("data", []):
            key = (str(s.get("exhibitor_id", "")), str(s.get("registration_id", "")))
            existing_map[key] = {"id": s["id"], "status": s.get("status", "pending")}

        # 4. Score each exhibitor × visitor
        suggestions_created = 0
        suggestions_updated = 0
        all_suggestions: list[ProfileMatchSuggestionOut] = []
        suggestions_by_exhibitor: dict[str, int] = {}
        SCORE_THRESHOLD = max(0.1, min(0.95, request.score_threshold))
        KEYWORD_THRESHOLD = max(0.0, min(0.5, request.keyword_threshold))
        MAX_CANDIDATES = max(5, min(200, request.max_candidates_per_exhibitor))

        for ex_profile in exhibitor_profiles:
            ex_id = ex_profile["exhibitor_id"]

            # Keyword prefilter
            scored_submissions = []
            for sub in visitor_submissions:
                visitor_profile = sub.get("_merged_profile") or {}
                if not visitor_profile:
                    continue
                kw_score = profile_keyword_prefilter_score(ex_profile, visitor_profile)
                if kw_score >= KEYWORD_THRESHOLD:
                    scored_submissions.append((kw_score, sub, visitor_profile))

            scored_submissions.sort(key=lambda x: x[0], reverse=True)
            top_submissions = scored_submissions[:MAX_CANDIDATES]

            # AI scoring — parallel
            async def _score_one(sub: dict, vp: dict, ep: dict = ex_profile):
                return sub, await score_profile_business_match(ep, vp, model=request.ai_model)

            score_tasks = [_score_one(s, vp) for _, s, vp in top_submissions]
            score_results = await asyncio.gather(*score_tasks, return_exceptions=True)

            for result in score_results:
                if isinstance(result, Exception):
                    continue
                submission, score_result = result
                registration_id = submission.get("registration_id")
                score = score_result["score"]
                if score < SCORE_THRESHOLD:
                    continue

                suggestion = ProfileMatchSuggestionOut(
                    exhibitor_id=str(ex_id),
                    registration_id=str(registration_id),
                    score=score,
                    matched_criteria=score_result["matched_criteria"],
                    ai_reasoning=score_result["ai_reasoning"],
                )
                all_suggestions.append(suggestion)

                key = (str(ex_id), str(registration_id))
                existing = existing_map.get(key)
                suggestion_data = {
                    "event_id": event_id,
                    "exhibitor_id": str(ex_id),
                    "registration_id": str(registration_id),
                    "score": round(score, 4),
                    "matched_criteria": suggestion.matched_criteria,
                    "ai_reasoning": suggestion.ai_reasoning,
                    "source": "profile_matching",
                }

                if existing:
                    if existing["status"] not in ("pending",):
                        continue
                    if not request.rescore_pending:
                        continue
                    await directus_patch(
                        f"/items/business_match_suggestions/{existing['id']}",
                        suggestion_data,
                    )
                    suggestions_updated += 1
                else:
                    await directus_post(
                        "/items/business_match_suggestions",
                        {**suggestion_data, "status": "pending"},
                    )
                    suggestions_created += 1
                    suggestions_by_exhibitor[str(ex_id)] = (
                        suggestions_by_exhibitor.get(str(ex_id), 0) + 1
                    )

        # 5. Notification
        if suggestions_by_exhibitor:
            try:
                event_resp = await directus_get(
                    f"/items/events/{event_id}?fields[]=user_created"
                )
                organizer_user_id = (event_resp.get("data") or {}).get("user_created")
                if organizer_user_id:
                    total_new = sum(suggestions_by_exhibitor.values())
                    await create_notification(
                        user_id=organizer_user_id,
                        title=f"{total_new} gợi ý profile matching mới từ AI",
                        body=f"{len(suggestions_by_exhibitor)} exhibitor(s) có đối tác tiềm năng mới (profile-based)",
                        link=f"{ADMIN_URL}/events/{event_id}/business-matching/ai",
                        notif_type="matching_complete",
                    )
            except Exception:
                pass

        total_candidates = len(visitor_submissions)
        return ProfileMatchRunResponse(
            success=True,
            message=(
                f"Profile matching complete. {suggestions_created} new, {suggestions_updated} refreshed. "
                f"Checked {len(exhibitor_profiles)} exhibitor(s) × top-{MAX_CANDIDATES} of "
                f"{total_candidates} candidates "
                f"(min score {int(SCORE_THRESHOLD*100)}%, keyword "
                f"{int(KEYWORD_THRESHOLD*100)}%, model {request.ai_model})."
            ),
            suggestions_created=suggestions_created,
            suggestions=all_suggestions,
        )

    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Directus error: {e.response.status_code} {e.response.text[:200]}",
        )
    except Exception as e:
        logger.exception("Profile business matching error")
        raise HTTPException(
            status_code=500, detail="Profile business matching failed"
        )
