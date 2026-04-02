"""
Invoice service — generates PDF invoices for Vietnamese subscription payments.
Phase A: HTML-to-PDF generation. Phase B (future): e-invoice provider API integration.
"""
import logging
from datetime import datetime, timezone

from app.services.directus import directus_get, directus_patch
from app.config import DIRECTUS_URL, DIRECTUS_ADMIN_TOKEN

logger = logging.getLogger(__name__)

# Nexpo seller info (platform-level, could move to config collection later)
SELLER_INFO = {
    "company_name": "Công ty TNHH Nexpo",
    "tax_id": "",  # Set when available
    "address": "TP. Hồ Chí Minh, Việt Nam",
    "email": "billing@nexpo.vn",
}


async def generate_invoice_for_payment(payment_id: str) -> dict | None:
    """Generate a PDF invoice for a subscription payment and upload to Directus."""
    # Fetch payment details
    payment = await directus_get(f"/items/subscription_payments/{payment_id}")
    payment_data = payment.get("data", {})
    if not payment_data:
        logger.error(f"Payment not found: {payment_id}")
        return None

    tenant_id = payment_data.get("tenant_id")
    amount = payment_data.get("amount", 0)
    currency = payment_data.get("currency", "VND")

    # Only generate for VND/PayOS payments (Polar has its own invoices)
    if payment_data.get("provider") == "polar":
        logger.info(f"Skipping invoice for Polar payment {payment_id} — Polar provides own invoices")
        return None

    # Fetch tenant billing info
    billing_info = await _get_billing_info(tenant_id)
    if not billing_info or not billing_info.get("tax_id"):
        logger.info(f"No billing info/tax_id for tenant {tenant_id} — skipping invoice")
        return None

    # Generate invoice number
    invoice_number = await _next_invoice_number(tenant_id)

    # Build invoice HTML
    html = _render_invoice_html(
        invoice_number=invoice_number,
        seller=SELLER_INFO,
        buyer=billing_info,
        payment=payment_data,
        amount=amount,
        currency=currency,
    )

    # Convert HTML to PDF using weasyprint (if available) or return HTML
    pdf_bytes = await _html_to_pdf(html)
    if not pdf_bytes:
        logger.warning("PDF generation failed — weasyprint may not be installed")
        return {"invoice_number": invoice_number, "html": html}

    # Upload PDF to Directus files
    import httpx
    filename = f"invoice-{invoice_number}.pdf"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DIRECTUS_URL}/files",
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"},
            files={"file": (filename, pdf_bytes, "application/pdf")},
            data={"title": f"Invoice {invoice_number}"},
        )
        if resp.status_code not in (200, 201):
            logger.error(f"Failed to upload invoice PDF: {resp.text}")
            return {"invoice_number": invoice_number, "html": html}

        file_data = resp.json().get("data", {})
        file_id = file_data.get("id")

    # Update payment record with invoice URL
    invoice_url = f"{DIRECTUS_URL}/assets/{file_id}"
    await directus_patch(f"/items/subscription_payments/{payment_id}", {
        "invoice_url": invoice_url,
    })

    logger.info(f"Invoice generated: {invoice_number} for payment {payment_id}")
    return {"invoice_number": invoice_number, "file_id": file_id, "url": invoice_url}


def _render_invoice_html(
    invoice_number: str,
    seller: dict,
    buyer: dict,
    payment: dict,
    amount: int,
    currency: str,
) -> str:
    """Render HTML invoice matching Vietnamese legal requirements."""
    vat_rate = 0.10
    amount_before_vat = round(amount / (1 + vat_rate))
    vat_amount = amount - amount_before_vat
    date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    description = payment.get("description", "Dịch vụ phần mềm Nexpo")

    return f"""<!DOCTYPE html>
<html lang="vi">
<head><meta charset="utf-8"><title>Hoá đơn {invoice_number}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 40px; color: #333; }}
  .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #4F80FF; padding-bottom: 20px; margin-bottom: 30px; }}
  .logo {{ font-size: 28px; font-weight: bold; color: #4F80FF; }}
  .invoice-info {{ text-align: right; }}
  .parties {{ display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-bottom: 30px; }}
  .party h3 {{ margin: 0 0 10px; color: #666; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  .party p {{ margin: 4px 0; font-size: 14px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
  th {{ background: #f8f9fa; text-align: left; padding: 10px 12px; font-size: 12px; text-transform: uppercase; color: #666; }}
  td {{ padding: 12px; border-bottom: 1px solid #eee; font-size: 14px; }}
  .totals {{ text-align: right; }}
  .totals td {{ border: none; }}
  .total-row td {{ font-weight: bold; font-size: 16px; color: #4F80FF; border-top: 2px solid #4F80FF; }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; font-size: 12px; color: #999; text-align: center; }}
</style></head>
<body>
  <div class="header">
    <div class="logo">NEXPO</div>
    <div class="invoice-info">
      <p><strong>Hoá đơn #{invoice_number}</strong></p>
      <p>Ngày: {date_str}</p>
    </div>
  </div>
  <div class="parties">
    <div class="party">
      <h3>Bên bán</h3>
      <p><strong>{seller['company_name']}</strong></p>
      <p>MST: {seller.get('tax_id', 'N/A')}</p>
      <p>{seller.get('address', '')}</p>
    </div>
    <div class="party">
      <h3>Bên mua</h3>
      <p><strong>{buyer.get('company_name', 'N/A')}</strong></p>
      <p>MST: {buyer.get('tax_id', 'N/A')}</p>
      <p>{buyer.get('billing_address', '')}</p>
    </div>
  </div>
  <table>
    <thead><tr><th>STT</th><th>Mô tả</th><th>SL</th><th>Đơn giá</th><th>Thành tiền</th></tr></thead>
    <tbody>
      <tr><td>1</td><td>{description}</td><td>1</td><td>{_format_vnd(amount_before_vat)}</td><td>{_format_vnd(amount_before_vat)}</td></tr>
    </tbody>
  </table>
  <table class="totals">
    <tr><td>Cộng tiền hàng:</td><td>{_format_vnd(amount_before_vat)}</td></tr>
    <tr><td>Thuế GTGT (10%):</td><td>{_format_vnd(vat_amount)}</td></tr>
    <tr class="total-row"><td>Tổng thanh toán:</td><td>{_format_vnd(amount)}</td></tr>
    <tr><td colspan="2" style="font-size:13px;color:#666;">Bằng chữ: {_number_to_vietnamese_words(amount)} đồng</td></tr>
  </table>
  <div class="footer">
    <p>© {datetime.now().year} Nexpo. Hoá đơn được tạo tự động.</p>
  </div>
</body></html>"""


async def _html_to_pdf(html: str) -> bytes | None:
    """Convert HTML to PDF bytes using weasyprint."""
    try:
        from weasyprint import HTML
        return HTML(string=html).write_pdf()
    except ImportError:
        logger.warning("weasyprint not installed — PDF generation unavailable")
        return None
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        return None


async def _get_billing_info(tenant_id: int) -> dict | None:
    """Get billing info for a tenant."""
    result = await directus_get(
        f"/items/tenant_billing_info?filter[tenant_id][_eq]={tenant_id}&limit=1"
    )
    data = result.get("data", [])
    return data[0] if data else None


async def _next_invoice_number(tenant_id: int) -> str:
    """Generate sequential invoice number: NXP-{year}-{tenant}-{seq}."""
    year = datetime.now().year
    prefix = f"NXP-{year}-{tenant_id}"
    result = await directus_get(
        f"/items/subscription_payments"
        f"?filter[tenant_id][_eq]={tenant_id}"
        f"&filter[invoice_url][_nnull]=true"
        f"&aggregate[count]=id"
    )
    count = 0
    agg = result.get("data", [])
    if agg and agg[0].get("count", {}).get("id"):
        count = int(agg[0]["count"]["id"])
    return f"{prefix}-{count + 1:04d}"


def _format_vnd(amount: int) -> str:
    """Format amount as VND string."""
    return f"{amount:,.0f} ₫".replace(",", ".")


def _number_to_vietnamese_words(n: int) -> str:
    """Convert number to Vietnamese words (covers 0 to ~999 billion)."""
    if n == 0:
        return "không"

    units = ["", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
    result_parts = []

    if n >= 1_000_000_000:
        billions = n // 1_000_000_000
        n %= 1_000_000_000
        result_parts.append(f"{_number_to_vietnamese_words(billions)} tỷ")

    if n >= 1_000_000:
        millions = n // 1_000_000
        n %= 1_000_000
        result_parts.append(f"{_three_digits_vn(millions, units)} triệu")

    if n >= 1_000:
        thousands = n // 1_000
        n %= 1_000
        result_parts.append(f"{_three_digits_vn(thousands, units)} nghìn")

    if n > 0:
        result_parts.append(_three_digits_vn(n, units))

    return " ".join(result_parts).strip()


def _three_digits_vn(n: int, units: list[str]) -> str:
    """Convert 1-999 to Vietnamese words."""
    if n == 0:
        return ""
    parts = []
    hundreds = n // 100
    tens = (n % 100) // 10
    ones = n % 10

    if hundreds > 0:
        parts.append(f"{units[hundreds]} trăm")
    if tens > 0:
        if tens == 1:
            parts.append("mười")
        else:
            parts.append(f"{units[tens]} mươi")
    elif hundreds > 0 and ones > 0:
        parts.append("lẻ")
    if ones > 0:
        if ones == 5 and tens > 0:
            parts.append("lăm")
        elif ones == 1 and tens > 1:
            parts.append("mốt")
        else:
            parts.append(units[ones])
    return " ".join(parts)
