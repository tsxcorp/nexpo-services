import io
import os
from typing import List, Optional, Any
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Schema definition for incoming payload
class Customer(BaseModel):
    model_config = ConfigDict(extra='ignore')
    customer_name: Optional[str] = None
    tax_code: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    representative: Optional[str] = None
    notes: Optional[str] = None

class ProductImage(BaseModel):
    model_config = ConfigDict(extra='ignore')
    url: Optional[str] = None

class QuoteItem(BaseModel):
    model_config = ConfigDict(extra='ignore')
    product_code: Optional[str] = None
    product_desc: Optional[str] = None
    season: Optional[str] = None
    color: Optional[str] = None
    total_quantity: Optional[int] = None
    selling_price: Optional[str] = None
    final_cost: Optional[str] = None
    product_image: Optional[List[ProductImage]] = []

class QuoteCommercial(BaseModel):
    model_config = ConfigDict(extra='ignore')
    quote_item: Optional[QuoteItem] = None

class PDFApiRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')
    quote_number_auto: Optional[str] = None
    order_type: Optional[str] = None
    customer: Optional[Customer] = None
    items: Optional[List[QuoteItem]] = []
    quote_commercial: Optional[List[QuoteCommercial]] = []

router = APIRouter(prefix="/nocobase-ex-pdf", tags=["export-pdf"])

def fetch_product_image_sync(url: str, width=1.8*cm, height=2.0*cm) -> Any:
    """Download image synchronously to inject into ReportLab"""
    if not url:
        return ""
    full_url = f"https://namkhoi.nexpo.vn{url}" if not url.startswith("http") else url
    try:
        resp = httpx.get(full_url, verify=False, timeout=5)
        if resp.status_code == 200:
            return RLImage(io.BytesIO(resp.content), width=width, height=height, kind='proportional')
    except Exception as e:
        print(f"Failed to fetch image {full_url}: {e}")
    return ""

def build_pdf_document(data: PDFApiRequest) -> bytes:
    buffer = io.BytesIO()
    
    # 1. Setup Document
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=A4, 
        rightMargin=1.5*cm, leftMargin=1.5*cm, 
        topMargin=2*cm, bottomMargin=2*cm
    )
    
    # 2. Setup Fonts
    font_path = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "Roboto-Regular.ttf")
    font_bold_path = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "Roboto-Bold.ttf")
    
    has_font = False
    if os.path.exists(font_path) and os.path.exists(font_bold_path):
        pdfmetrics.registerFont(TTFont('Roboto', font_path))
        pdfmetrics.registerFont(TTFont('Roboto-Bold', font_bold_path))
        has_font = True
    
    FONT_NORMAL = "Roboto" if has_font else "Helvetica"
    FONT_BOLD = "Roboto-Bold" if has_font else "Helvetica-Bold"
    
    # 3. Setup Styles
    styles = getSampleStyleSheet()
    
    style_normal = ParagraphStyle('Normal_Vi', fontName=FONT_NORMAL, fontSize=9, leading=12)
    style_bold = ParagraphStyle('Bold_Vi', fontName=FONT_BOLD, fontSize=9, leading=12)
    style_center_bold = ParagraphStyle('Center_Bold_Vi', fontName=FONT_BOLD, fontSize=9, alignment=TA_CENTER)
    style_center = ParagraphStyle('Center_Vi', fontName=FONT_NORMAL, fontSize=9, alignment=TA_CENTER)
    style_title = ParagraphStyle('Title_Vi', fontName=FONT_BOLD, fontSize=16, alignment=TA_CENTER, leading=20, spaceAfter=5)
    
    elements = []
    
    # --- HEADER SECTION ---
    elements.append(Paragraph("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", style_center_bold))
    elements.append(Paragraph("ĐỘC LẬP - TỰ DO - HẠNH PHÚC", style_center_bold))
    elements.append(Paragraph("---------oOo--------", style_center_bold))
    elements.append(Spacer(1, 0.5*cm))
    
    # TITLE: ĐƠN ĐẶT HÀNG
    elements.append(Paragraph("ĐƠN ĐẶT HÀNG", style_title))
    quote_number = data.quote_number_auto or ".................."
    elements.append(Paragraph(f"Số: {quote_number}", style_center_bold))
    elements.append(Spacer(1, 1*cm))
    
    # --- VENDOR & BUYER SECTION ---
    cust = data.customer or Customer()
    
    vendor_info = [
        Paragraph("<b>BÊN BÁN : CÔNG TY TNHH NK FLEXIBLE</b>", style_normal),
        Paragraph("Địa chỉ: 12B Đường Thạnh lộc 07, phường Thạnh Lộc, Q.12, TP HCM", style_normal),
        Paragraph("Mã số thuế: 0317898840", style_normal),
        Paragraph("Điện thoại: 0967186179", style_normal),
        Paragraph("Đại điện bởi: (Ông) NGUYỄN TRỌNG NGHĨA", style_normal),
    ]
    
    buyer_info = [
        Paragraph(f"<b>BÊN MUA: {cust.customer_name or '................'}</b>", style_normal),
        Paragraph(f"Địa chỉ: {cust.address or '................'}", style_normal),
        Paragraph(f"Mã số thuế: {cust.tax_code or '................'}", style_normal),
        Paragraph(f"Điện thoại: {cust.phone or '................'}", style_normal),
        Paragraph(f"Đại điện bởi: {cust.representative or '................'}", style_normal),
    ]
    
    t_parties = Table([[vendor_info, buyer_info]], colWidths=[9.0*cm, 9.0*cm])
    t_parties.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(t_parties)
    elements.append(Spacer(1, 1*cm))
    
    # --- TABLE SECTION ---
    elements.append(Paragraph("<b>I. HÀNG HÓA, SỐ LƯỢNG, ĐƠN GIÁ, TỔNG GIÁ TRỊ:</b>", style_normal))
    elements.append(Spacer(1, 0.3*cm))
    
    # Top two rows for the complex header
    row_1 = [
        Paragraph("<b>STT</b>", style_center_bold),
        Paragraph("<b>BST</b>", style_center_bold),
        Paragraph("<b>MÃ HÀNG</b>", style_center_bold),
        Paragraph("<b>HÌNH ẢNH</b>", style_center_bold),
        Paragraph("<b>Màu</b>", style_center_bold),
        Paragraph("<b>SỐ LƯỢNG<br/>(Pcs)</b>", style_center_bold),
        Paragraph("<b>SỐ LƯỢNG THEO TỶ LỆ (Pcs)</b>", style_center_bold), "", "", "",
        Paragraph("<b>ĐƠN GIÁ<br/>(Vnd/Pcs)</b>", style_center_bold),
        Paragraph("<b>THÀNH TIỀN<br/>(Vnd)</b>", style_center_bold),
    ]
    row_2 = [
        "", "", "", "", "", "",
        Paragraph("Size S", style_center),
        Paragraph("Size M", style_center),
        Paragraph("Size L", style_center),
        Paragraph("Size XL", style_center),
        "", ""
    ]
    table_data = [row_1, row_2]
    
    total_amount = 0.0
    
    # Extract items
    processed_items = []
    if data.quote_commercial and len(data.quote_commercial) > 0:
        for qc in data.quote_commercial:
            if qc.quote_item: processed_items.append(qc.quote_item)
    else:
        for it in data.items: processed_items.append(it)
        
    for i, it in enumerate(processed_items):
        qty = float(it.total_quantity or 0)
        price = float(it.selling_price or it.final_cost or 0)
        amount = qty * price
        total_amount += amount
        
        # Load image if url available
        img_obj = ""
        if it.product_image and len(it.product_image) > 0:
            url = it.product_image[0].url
            img_obj = fetch_product_image_sync(url)
        
        table_data.append([
            Paragraph(str(i+1), style_center_bold),
            Paragraph(it.season or "", style_center),
            Paragraph(it.product_code or "", style_center),
            img_obj,
            Paragraph(it.color or "", style_center),
            Paragraph(f"{qty:,.0f}" if qty else "0", style_center),
            Paragraph("0", style_center), # Size S
            Paragraph("0", style_center), # Size M
            Paragraph("0", style_center), # Size L
            Paragraph("0", style_center), # Size XL
            Paragraph(f"{price:,.0f}" if price else "0", style_center),
            Paragraph(f"{amount:,.0f}" if amount else "0", style_center_bold),
        ])
    
    # Subtotal rows (aligning right using cell merges)
    vat_rate = 0.08  # 8%
    vat_amount = total_amount * vat_rate
    grand_total = total_amount + vat_amount
    
    # We pad the first 10 columns with "", put title at col 10, value at col 11
    # Actually wait, we will span col 0 to col 10 and put value at 11
    table_data.append([
        Paragraph("<b>TỔNG CỘNG TRƯỚC THUẾ</b>", style_center_bold),
        "", "", "", "", "", "", "", "", "", "",
        Paragraph(f"<b>{total_amount:,.0f}</b>", style_center_bold)
    ])
    table_data.append([
        Paragraph("<b>THUẾ VAT 8%</b>", style_center_bold),
        "", "", "", "", "", "", "", "", "", "",
        Paragraph(f"<b>{vat_amount:,.0f}</b>", style_center_bold)
    ])
    table_data.append([
        Paragraph("<b>TỔNG CỘNG SAU THUẾ</b>", style_center_bold),
        "", "", "", "", "", "", "", "", "", "",
        Paragraph(f"<b>{grand_total:,.0f}</b>", style_center_bold)
    ])
    
    # Col Widths sum = 18cm (0.8+1.5+2.0+2.0+1.2+1.5+1.25*4+1.7+2.3 = 18.0)
    col_widths = [0.8*cm, 1.5*cm, 2.0*cm, 2.0*cm, 1.2*cm, 1.5*cm, 1.25*cm, 1.25*cm, 1.25*cm, 1.25*cm, 1.7*cm, 2.3*cm]
    t_items = Table(table_data, colWidths=col_widths, repeatRows=2)
    
    ts = TableStyle([
        ('BACKGROUND', (0,0), (-1,1), colors.HexColor("#dae3f3")), # light blue shade like excel
        ('TEXTCOLOR', (0,0), (-1,-1), colors.black),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        
        # Header Spacing
        ('SPAN', (0,0), (0,1)),
        ('SPAN', (1,0), (1,1)),
        ('SPAN', (2,0), (2,1)),
        ('SPAN', (3,0), (3,1)),
        ('SPAN', (4,0), (4,1)),
        ('SPAN', (5,0), (5,1)),
        ('SPAN', (6,0), (9,0)), # SỐ LƯỢNG THEO TỶ LỆ spans S,M,L,XL
        ('SPAN', (10,0), (10,1)),
        ('SPAN', (11,0), (11,1)),
        
        # Footer Spacing
        ('SPAN', (0, -3), (10, -3)), # Total before tax
        ('SPAN', (0, -2), (10, -2)), # VAT
        ('SPAN', (0, -1), (10, -1)), # Total after tax
    ])
    t_items.setStyle(ts)
    
    elements.append(t_items)
    elements.append(Spacer(1, 0.5*cm))
    
    elements.append(Paragraph("* Đơn giá chưa bao gồm VAT.", style_normal))
    elements.append(Spacer(1, 1*cm))
    
    # --- TERMS SECTION ---
    elements.append(Paragraph("<b>II. GIAO HÀNG:</b>", style_normal))
    elements.append(Paragraph("- Đóng trong túi bóng thành kiện theo chuẩn sản xuất.", style_normal))
    elements.append(Spacer(1, 0.5*cm))
    
    elements.append(Paragraph("<b>III. THANH TOÁN:</b>", style_normal))
    elements.append(Paragraph("- Dựa theo thỏa thuận hợp đồng: Ứng trước 30% khi chốt PO.", style_normal))
    elements.append(Spacer(1, 0.5*cm))
    
    elements.append(Paragraph("<b>IV. ĐIỀU KHOẢN CHUNG:</b>", style_normal))
    elements.append(Paragraph("- Đơn đặt hàng này không thể tách rời hợp đồng.", style_normal))
    elements.append(Spacer(1, 1*cm))
    
    # --- SIGNATURES ---
    sig_data = [
        [
            Paragraph("<b>ĐẠI DIỆN BÊN BÁN</b>", style_center_bold),
            Paragraph("<b>ĐẠI DIỆN BÊN MUA</b>", style_center_bold)
        ],
        [ Spacer(1, 2.5*cm), Spacer(1, 2.5*cm) ],
        [
            Paragraph("<b>NGUYỄN TRỌNG NGHĨA</b>", style_center_bold),
            Paragraph(f"<b>{cust.representative or '................'}</b>", style_center_bold)
        ]
    ]
    t_sigs = Table(sig_data, colWidths=[9*cm, 9*cm])
    elements.append(t_sigs)
    
    doc.build(elements)
    buffer.seek(0)
    return buffer.read()

@router.post("")
async def export_pdf(data: PDFApiRequest):
    try:
        pdf_bytes = build_pdf_document(data)
        
        pdf_stream = io.BytesIO(pdf_bytes)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        headers = {
            'Content-Disposition': f'attachment; filename="PO_export_{timestamp}.pdf"'
        }
        return StreamingResponse(
            pdf_stream, 
            media_type="application/pdf", 
            headers=headers
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
