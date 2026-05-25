import httpx, base64, time, logging
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

def booking_id() -> str: return "BK" + str(int(time.time()*1000))[-10:]
def request_id() -> str: return "SR" + str(int(time.time()*1000))[-10:]
def ist_now() -> datetime: return datetime.now(IST)

def fmt_date(d) -> str:
    if not d: return "—"
    try:
        if isinstance(d, str): d = datetime.fromisoformat(d.replace("Z","+00:00"))
        return d.astimezone(IST).strftime("%d %b %Y")
    except: return str(d).split("T")[0]

def calc_nights(ci, co) -> int:
    try:
        if isinstance(ci, str): ci = datetime.strptime(ci.split("T")[0], "%Y-%m-%d")
        if isinstance(co, str): co = datetime.strptime(co.split("T")[0], "%Y-%m-%d")
        return max((co.date()-ci.date()).days, 1)
    except: return 1

def categorize_service(name: str) -> tuple:
    s = name.lower()
    if any(w in s for w in ["food","breakfast","lunch","dinner","coffee","tea","snack","juice","water bottle","cold drink","minibar","meal","biryani","roti","rice"]):
        return "Food", "Kitchen"
    if any(w in s for w in ["laundry","wash","iron","press","dry clean"]):
        return "Laundry", "Laundry"
    if any(w in s for w in ["clean","housekeeping","towel","pillow","toiletri","amenity","extra bed","blanket"]):
        return "Housekeeping", "Housekeeping"
    if any(w in s for w in ["cab","taxi","car","airport","transport","pick","drop"]):
        return "Transport", "Reception"
    return "Other", "Reception"

async def html_to_pdf_b64(html: str, gotenberg_url: str) -> str:
    url = f"{gotenberg_url.rstrip('/')}/forms/chromium/convert/html"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, files={"files": ("index.html", html.encode(), "text/html")})
            if r.status_code == 200: return base64.b64encode(r.content).decode()
    except Exception as e:
        logger.error(f"PDF error: {e}")
    return ""

def build_bill_html(booking: dict, charges: list, hotel: dict) -> str:
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    name = booking.get("guest_name","Guest")
    room = booking.get("room_number","")
    bid  = booking.get("booking_id","")
    ci   = fmt_date(booking.get("checkin_date"))
    co   = fmt_date(booking.get("checkout_date"))
    hn   = hotel.get("hotel_name","Hotel")
    logo = hotel.get("logo_url","")
    pri  = hotel.get("primary_color","#c8a84b")
    sec  = hotel.get("secondary_color","#1a2942")
    addr = hotel.get("address","")
    city = hotel.get("city","")
    ph   = hotel.get("hotel_whatsapp") or hotel.get("emergency_number","")
    tag  = hotel.get("tagline","")

    by_date: dict = {}
    grand = paid = 0.0
    for c in charges:
        dk = str(c.get("charge_date","")).split("T")[0]
        by_date.setdefault(dk,[]).append(c)
        grand += float(c.get("total",0))
        if c.get("payment_status") == "Paid": paid += float(c.get("total",0))

    rows = ""
    for dk in sorted(by_date.keys()):
        dt = sum(float(c.get("total",0)) for c in by_date[dk])
        rows += f'<tr class="dh"><td colspan="3">{fmt_date(dk)}</td><td style="text-align:right">₹{dt:,.0f}</td></tr>'
        for c in by_date[dk]:
            ic = "✓" if c.get("payment_status")=="Paid" else "●"
            rows += f'<tr><td><span class="tg">{c.get("service_type","")}</span></td><td>{c.get("description","")}</td><td style="text-align:center">{ic}</td><td style="text-align:right">₹{float(c.get("total",0)):,.0f}</td></tr>'

    balance = max(grand-paid,0)
    bc = "#e74c3c" if balance>0 else "#27ae60"
    bt = f"₹{balance:,.0f}" if balance>0 else "✓ FULLY PAID"
    logo_h = f'<img src="{logo}" style="height:55px;object-fit:contain;margin-bottom:7px"><br>' if logo else ""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:Arial,sans-serif;font-size:13px;color:#333;background:#fff;padding:22px;}}
.hdr{{text-align:center;border-bottom:3px solid {pri};padding-bottom:14px;margin-bottom:18px;}}
.hdr h1{{font-size:21px;color:{sec};letter-spacing:.5px;}}
.hdr .sub{{color:{pri};font-size:11px;}}
.ig{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px;}}
.ib{{background:#f8f6f0;padding:10px;border-radius:4px;}}
.ib label{{font-size:10px;text-transform:uppercase;color:#999;letter-spacing:.5px;}}
.ib p{{font-weight:bold;margin-top:2px;}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;}}
th{{background:{sec};color:{pri};padding:8px 7px;text-align:left;font-size:11px;text-transform:uppercase;}}
td{{padding:6px 7px;border-bottom:1px solid #eee;font-size:12px;}}
.dh td{{background:#f0ece0;font-weight:bold;color:{sec};font-size:11px;padding:5px 7px;}}
.tg{{background:{sec};color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;}}
.tot{{float:right;width:240px;}}
.tot td{{padding:5px 7px;font-size:12px;}}
.grand{{background:{sec};color:{pri};font-weight:bold;font-size:13px;}}
.ftr{{clear:both;text-align:center;margin-top:26px;padding-top:12px;border-top:1px solid #ddd;color:#999;font-size:11px;}}
</style></head><body>
<div class="hdr">
  {logo_h}<h1>{hn}</h1><div class="sub">{tag}</div>
  <div style="font-size:11px;color:#888;margin-top:3px">{addr}{", "+city if city else ""}{(" · "+ph) if ph else ""}</div>
  <div style="font-size:11px;color:#888;margin-top:2px">GUEST FOLIO · {now}</div>
</div>
<div class="ig">
  <div class="ib"><label>Guest</label><p>{name}</p></div>
  <div class="ib"><label>Room</label><p>{room}</p></div>
  <div class="ib"><label>Check-in</label><p>{ci}</p></div>
  <div class="ib"><label>Check-out</label><p>{co}</p></div>
  <div class="ib"><label>Booking ID</label><p style="font-family:monospace;font-size:11px">{bid}</p></div>
  <div class="ib"><label>Payment</label><p>{booking.get("payment_mode","—")}</p></div>
</div>
<table>
  <thead><tr><th>Category</th><th>Description</th><th style="text-align:center">Status</th><th style="text-align:right">Amount</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="4" style="text-align:center;color:#999;padding:14px">No charges on file</td></tr>'}</tbody>
</table>
<div class="tot"><table>
  <tr><td>Subtotal</td><td style="text-align:right">₹{grand:,.0f}</td></tr>
  <tr><td>Amount Paid</td><td style="text-align:right">₹{paid:,.0f}</td></tr>
  <tr><td style="color:{bc}">Balance Due</td><td style="text-align:right;color:{bc}"><b>{bt}</b></td></tr>
  <tr class="grand"><td>GRAND TOTAL</td><td style="text-align:right">₹{grand:,.0f}</td></tr>
</table></div>
<div class="ftr"><p>Thank you for staying at {hn}! 🙏</p><p style="margin-top:3px">Computer-generated bill · {hn}</p></div>
</body></html>"""
