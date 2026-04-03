"""
Custom auth endpoints — password reset via Mailgun (bypasses Directus email).

POST /auth/request-reset     — send reset email
POST /auth/validate-reset-token — check if token is valid
POST /auth/reset-password    — reset password with token
"""
from fastapi import APIRouter
from app.models.schemas import (
    PasswordResetRequest,
    PasswordResetResponse,
    ValidateResetTokenRequest,
    ValidateResetTokenResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
)
from app.services.password_reset_service import (
    request_password_reset,
    validate_reset_token,
    reset_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/request-reset", response_model=PasswordResetResponse)
async def request_reset(req: PasswordResetRequest):
    """Request password reset email. Always returns success (no email leak)."""
    await request_password_reset(req.email, req.app)
    return PasswordResetResponse(success=True, message="If this email is registered, you'll receive a reset link.")


@router.post("/validate-reset-token", response_model=ValidateResetTokenResponse)
async def validate_token(req: ValidateResetTokenRequest):
    """Validate a reset token before showing the form."""
    result = await validate_reset_token(req.token)
    return ValidateResetTokenResponse(**result)


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def do_reset_password(req: ResetPasswordRequest):
    """Reset password using a valid token."""
    result = await reset_password(req.token, req.new_password)
    return ResetPasswordResponse(**result)
