"""
Invoice endpoints — generate and download subscription invoices.
"""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.invoice_service import generate_invoice_for_payment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/invoices", tags=["invoices"])


class GenerateInvoiceRequest(BaseModel):
    payment_id: str


@router.post("/generate")
async def generate_invoice(req: GenerateInvoiceRequest):
    """Generate a PDF invoice for a subscription payment."""
    result = await generate_invoice_for_payment(req.payment_id)
    if not result:
        raise HTTPException(status_code=404, detail="Payment not found or invoice not applicable")
    return result
