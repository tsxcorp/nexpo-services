# nexpo-services — Backlog

> Không commit lên git. Claude tự cập nhật cuối mỗi session.
> Cross-project decisions: xem `nexpo-platform/.claude/PROGRESS.md`

---

## ✅ Đã làm xong

### [2026-03] CORS fix
- Thêm `portal.nexpo.vn`, `insights.nexpo.vn`, ports 3002/3003 vào allow_origins

---

## 🔄 In Progress / Chưa xong

_(không có task đang dở)_

---

## 📋 Backlog

- [ ] Refactor `main.py` thành router modules (`routers/qr.py`, `routers/email.py`, v.v.)
- [ ] `POST /send-email-with-qr`: thêm param `link_type: "registration"|"ticket"` — ảnh hưởng URL trong `inject_qr_extras()`
- [ ] Thêm `POST /send-email` — gửi email không có QR (dùng cho payment failed, order expired)
- [ ] APScheduler: expire pending ticket orders mỗi 5 phút (cần `apscheduler==3.10.4`)
- [ ] `POST /generate-email-template`: thêm case `form_purpose = "ticket_confirmation"`
