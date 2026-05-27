import json
import hmac
import secrets
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from services import database as db
from services.cache import (get_session, set_session, set_room, calc_ttl,
                             get_room as cache_get_room, rate_limit_check)
from services.whatsapp import send_text, send_to_phones
from services.helpers import (booking_id as gen_bk, calc_nights, fmt_date, ist_now,
                              categorize_service, request_id as gen_sr,
                              html_escape, safe_color, safe_font, safe_url)
from datetime import date
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


async def _active_hotel_or_block(slug: str) -> dict:
    """Resolve a hotel by slug for guest-facing pages.

    * 404 if the slug is unknown.
    * 503 + a friendly message if the hotel is paused / deactivated. The
      message includes the auto-resume time when one is scheduled, so a
      guest who scans the QR while the place is on a fixed pause sees
      *when* it'll reopen rather than a generic "not found" wall.

    Admin / staff routes deliberately keep using `db.get_hotel_by_slug`
    directly so operators can still manage a paused hotel.
    """
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    if not hotel.get("is_active"):
        until = hotel.get("paused_until")
        reason = (hotel.get("paused_reason") or "").strip()
        msg = f"{hotel.get('hotel_name','This hotel')} is temporarily unavailable."
        if until:
            try:
                # Stored as naive UTC; convert back to IST for the guest.
                from datetime import timedelta
                ist = until + timedelta(hours=5, minutes=30)
                msg += f" Reopens around {ist.strftime('%d %b %Y, %H:%M IST')}."
            except Exception:
                pass
        if reason:
            msg += f" ({reason})"
        raise HTTPException(503, msg)
    return hotel


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit bucketing.

    Behind a reverse proxy, `request.client.host` is the proxy itself —
    so we honour `X-Forwarded-For` (first hop) when present. This is a
    simple input for non-billing decisions; a malicious operator could
    spoof the header, but the per-slug+phone bucket below provides a
    second axis that is not spoofable.
    """
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff[:64]
    return (request.client.host if request.client else "unknown")[:64]


async def _get_guest_token_session(phone: str, token: str):
    """Load the Redis session for `phone` and verify it carries a guest_token
    matching `token` (constant time). Returns the session dict on success,
    None otherwise.

    Sessions created BEFORE this PR don't carry a `guest_token`. To avoid
    locking those guests out for the rest of their stay (TTL up to 24h),
    callers MAY treat a tokenless session as "legacy" and fall back to
    the previous phone+slug+active-booking behaviour — see how each guest
    endpoint handles the `legacy` return value below.

    Returns:
        ("ok", session_dict)   — token matches, full access
        ("legacy", session)    — session predates this PR, no token to check
        ("none", None)         — no session for this phone
        ("bad", None)          — session exists with a token, but submitted
                                  token is missing/wrong
    """
    sess = await get_session(phone)
    if not sess:
        return "none", None
    expected = sess.get("guest_token") or ""
    if not expected:
        return "legacy", sess
    submitted = (token or "").encode("utf-8")
    if not submitted or not hmac.compare_digest(expected.encode("utf-8"), submitted):
        return "bad", None
    return "ok", sess


def themed(hotel: dict, title: str, body: str) -> str:
    # All hotel branding fields are admin-editable. They were previously
    # interpolated raw, allowing stored XSS / CSS injection from the master
    # admin into every guest page (`/register/{slug}`, `/menu/{slug}`,
    # `/bill/{slug}`, `/food/{slug}`). Strict per-field sanitization here
    # is the choke point — `body` is already HTML composed by the caller
    # who is responsible for escaping its own dynamic data.
    pri = safe_color(hotel.get("primary_color"),    "#c8a84b")
    sec = safe_color(hotel.get("secondary_color"),  "#1a2942")
    bg  = safe_color(hotel.get("background_color"), "#0d1117")
    btn = safe_color(hotel.get("button_color"),     "#c8a84b")
    txt = safe_color(hotel.get("text_color"),       "#ffffff")
    fnt = safe_font(hotel.get("font_choice"), "Outfit")
    logo = safe_url(hotel.get("logo_url", ""))
    hn   = html_escape(hotel.get("hotel_name", "Hotel"))
    tag  = html_escape(hotel.get("tagline", ""))
    title_h = html_escape(title)
    addr = html_escape(hotel.get("address", ""))
    city = html_escape(hotel.get("city", ""))
    em   = html_escape(hotel.get("emergency_number", ""))
    maps = safe_url(hotel.get("google_maps_url", ""))
    email = html_escape(hotel.get("hotel_email", ""))
    ci_t = html_escape(hotel.get("check_in_time", "2:00 PM"))
    co_t = html_escape(hotel.get("checkout_time_display", "11:00 AM"))
    logo_h = f'<img src="{logo}" alt="{hn}" style="height:60px;object-fit:contain;display:block;margin:0 auto 10px">' if logo else ""
    maps_h = f'<a href="{maps}" target="_blank" rel="noopener noreferrer" style="color:{pri};font-size:12px">📍 Get Directions</a>' if maps else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>{title_h} — {hn}</title>
<link href="https://fonts.googleapis.com/css2?family={fnt.replace(' ','+')}:wght@300;400;500;600&family=Playfair+Display:wght@600&display=swap" rel="stylesheet">
<style>
:root{{--p:{pri};--s:{sec};--bg:{bg};--btn:{btn};--t:{txt};}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'{fnt}',sans-serif;background:var(--bg);color:var(--t);min-height:100vh;}}
.hdr{{background:var(--s);padding:18px 16px 14px;text-align:center;border-bottom:3px solid var(--p);}}
.hdr h1{{font-family:'Playfair Display',serif;font-size:21px;color:var(--p);}}
.hdr .sub{{font-size:12px;opacity:.7;margin-top:3px;}}
.wrap{{max-width:480px;margin:0 auto;padding:18px 14px;}}
.card{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:18px;margin-bottom:14px;}}
.ct{{font-size:13px;color:var(--p);font-weight:600;margin-bottom:12px;}}
label{{font-size:12px;color:rgba(255,255,255,.55);display:block;margin:10px 0 3px;}}
input,select,textarea{{width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:var(--t);padding:10px 12px;border-radius:8px;font-size:14px;font-family:'{fnt}',sans-serif;transition:border .15s;}}
input:focus,select:focus{{outline:none;border-color:var(--p);}}
select option{{background:#1a1a2e;}}
.btn{{width:100%;padding:13px;background:var(--btn);color:#000;font-size:15px;font-weight:700;border:none;border-radius:9px;cursor:pointer;font-family:'{fnt}',sans-serif;margin-top:14px;transition:opacity .15s;}}
.btn:hover{{opacity:.9;}} .btn:disabled{{opacity:.5;cursor:not-allowed;}}
.finfo{{background:rgba(255,255,255,.04);border-radius:9px;padding:12px;margin-top:14px;font-size:12px;}}
.frow{{display:flex;align-items:center;gap:7px;padding:4px 0;color:rgba(255,255,255,.65);}}
.toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%) translateY(80px);background:rgba(0,0,0,.92);border:1px solid var(--p);color:var(--t);padding:11px 20px;border-radius:8px;font-size:13px;z-index:9999;transition:transform .3s;text-align:center;max-width:88%;}}
.toast.show{{transform:translateX(-50%) translateY(0);}}
.scard{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:11px;margin-bottom:7px;cursor:pointer;transition:border .15s;}}
.scard:hover{{border-color:var(--p);}}
.ctitle{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--p);margin:12px 0 5px;font-weight:600;}}
</style></head><body>
<div class="hdr">{logo_h}<h1>{hn}</h1><div class="sub">{tag}</div></div>
<div class="wrap">
{body}
<div class="finfo">
  {f'<div class="frow">📍 {addr}{", "+city if city else ""} {maps_h}</div>' if addr else ""}
  {f'<div class="frow">📞 Emergency: {em}</div>' if em else ""}
  {f'<div class="frow">✉️ {email}</div>' if email else ""}
  <div class="frow">⏰ Check-in: {ci_t} &nbsp;|&nbsp; Checkout: {co_t}</div>
</div></div>
<div class="toast" id="toast"></div>
<script>
function showToast(m,ok=true){{const t=document.getElementById('toast');t.textContent=m;t.style.borderColor=ok?'var(--p)':'#e74c3c';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),4000);}}
</script></body></html>"""


# ── REGISTRATION PAGE ─────────────────────────────────────────────
@router.get("/register/{slug}", response_class=HTMLResponse)
async def reg_page(slug: str, request: Request):
    hotel = await _active_hotel_or_block(slug)
    hid = hotel["id"]
    rooms = await db.get_all_rooms(hid)
    room_opts = ""
    for r in rooms:
        occ = await cache_get_room(r["room_number"])
        if not occ:
            rn  = html_escape(r["room_number"])
            rt  = html_escape(r.get("room_type", ""))
            rs  = html_escape(r.get("qr_secret", "") or "")
            rr  = float(r.get("room_rate") or 0)
            room_opts += f'<option value="{rn}" data-secret="{rs}" data-rate="{rr}">{rn} — {rt} (₹{rr:.0f}/night)</option>'

    body = f"""
<div class="card"><div class="ct">🏨 Guest Registration</div>
  <p style="font-size:13px;opacity:.65">{html_escape(hotel.get("welcome_message","Welcome! Please fill your details."))}</p>
</div>
<div class="card"><div class="ct">🛏️ Room & Dates</div>
  <label>Select Room *</label>
  <select id="roomSel" onchange="onRoom()"><option value="">-- Select Room --</option>{room_opts}</select>
  <div id="rateInfo" style="display:none;margin-top:8px;font-size:12px;opacity:.6"></div>
  <label>Check-in Date *</label>
  <input type="date" id="ciDate" min="{ist_now().strftime('%Y-%m-%d')}" value="{ist_now().strftime('%Y-%m-%d')}">
  <label>Check-out Date *</label>
  <input type="date" id="coDate" min="{ist_now().strftime('%Y-%m-%d')}">
</div>
<div class="card"><div class="ct">👤 Primary Guest</div>
  <label>WhatsApp Number *</label><input type="tel" id="gPhone" placeholder="91XXXXXXXXXX">
  <div id="welcomeBack" style="display:none;background:rgba(200,168,75,.15);border:1px solid var(--p);color:var(--p);border-radius:7px;padding:8px 12px;font-size:12px;margin:4px 0 6px"></div>
  <label>Full Name *</label><input type="text" id="gName" placeholder="As per ID proof">
  <label>Alternate Phone</label><input type="tel" id="gAlt" placeholder="Optional">
  <label>Total Number of Guests *</label><input type="number" id="gCount" value="1" min="1" max="10" onchange="updateExtra()">
  <label>Customer GSTIN (optional, for B2B tax invoice)</label>
  <input type="text" id="gGstin" placeholder="22AAAAA0000A1Z5" maxlength="15" style="text-transform:uppercase">
</div>
<div class="card"><div class="ct">🪪 ID Proof</div>
  <label>ID Type *</label>
  <select id="idType"><option value="">-- Select --</option>
    <option>Aadhaar Card</option><option>PAN Card</option>
    <option>Passport</option><option>Driving License</option><option>Voter ID</option>
  </select>
  <label>ID Number *</label><input type="text" id="idNum" placeholder="Enter number">
  <label>ID Photo — Front *</label>
  <input type="file" id="idF" accept="image/*" capture="environment" onchange="upload(this,'idFUrl','idFPrev')">
  <div id="idFPrev"></div><input type="hidden" id="idFUrl">
  <label>ID Photo — Back</label>
  <input type="file" id="idB" accept="image/*" capture="environment" onchange="upload(this,'idBUrl','idBPrev')">
  <div id="idBPrev"></div><input type="hidden" id="idBUrl">
</div>
<div id="extraGuests"></div>
<button class="btn" id="subBtn" onclick="submit()">✅ Complete Registration</button>
<script>
const CLOUD={json.dumps(hotel.get('cloudinary_cloud_name','') or '')},PRESET={json.dumps(hotel.get('cloudinary_upload_preset','') or '')},SLUG={json.dumps(slug)};

function onRoom(){{
  const o=document.getElementById('roomSel').selectedOptions[0];
  const ri=document.getElementById('rateInfo');
  if(o&&o.value){{ri.style.display='block';ri.textContent='₹'+o.dataset.rate+'/night';}}
  else ri.style.display='none';
}}

function updateExtra(){{
  const n=parseInt(document.getElementById('gCount').value)||1;
  let h='';
  for(let i=2;i<=n;i++)h+=`<div class="card"><div class="ct">👤 Guest ${{i}}</div>
    <label>Full Name</label><input id="ag_n_${{i}}" placeholder="Name">
    <label>ID Type</label><select id="ag_t_${{i}}"><option value="">--Select--</option>
    <option>Aadhaar Card</option><option>PAN Card</option><option>Passport</option><option>Driving License</option></select>
    <label>ID Number</label><input id="ag_id_${{i}}" placeholder="ID number">
    <label>ID Photo Front</label>
    <input type="file" id="ag_f_${{i}}" accept="image/*" capture="environment" onchange="upload(this,'ag_fu_${{i}}','ag_fp_${{i}}')">
    <div id="ag_fp_${{i}}"></div><input type="hidden" id="ag_fu_${{i}}">
  </div>`;
  document.getElementById('extraGuests').innerHTML=h;
}}

async function upload(inp,urlId,prevId){{
  if(!CLOUD||!PRESET){{showToast('Cloudinary not configured',false);return;}}
  if(!inp.files||!inp.files[0])return;
  showToast('Uploading...');
  const fd=new FormData();fd.append('file',inp.files[0]);fd.append('upload_preset',PRESET);fd.append('folder','hotel-id-proofs');
  try{{
    const r=await fetch(`https://api.cloudinary.com/v1_1/${{CLOUD}}/image/upload`,{{method:'POST',body:fd}});
    const d=await r.json();
    if(d.secure_url){{
      document.getElementById(urlId).value=d.secure_url;
      const p=document.getElementById(prevId);
      if(p)p.innerHTML=`<img src="${{d.secure_url}}" style="width:100%;max-height:110px;object-fit:cover;border-radius:6px;margin-top:5px">`;
      showToast('Photo uploaded ✓');
    }}else showToast('Upload failed',false);
  }}catch(e){{showToast('Upload error',false);}}
}}

async function submit(){{
  const room=document.getElementById('roomSel').value;
  const ci=document.getElementById('ciDate').value;
  const co=document.getElementById('coDate').value;
  const name=document.getElementById('gName').value.trim();
  const phone=document.getElementById('gPhone').value.trim();
  const idType=document.getElementById('idType').value;
  const idNum=document.getElementById('idNum').value.trim();
  const idPhoto=document.getElementById('idFUrl').value;
  const count=parseInt(document.getElementById('gCount').value)||1;
  if(!room){{showToast('Select a room',false);return;}}
  if(!ci||!co||co<=ci){{showToast('Select valid check-in & checkout dates',false);return;}}
  if(!name){{showToast('Enter guest name',false);return;}}
  if(!phone||phone.length<10){{showToast('Enter valid WhatsApp number',false);return;}}
  if(!idType){{showToast('Select ID type',false);return;}}
  if(!idNum){{showToast('Enter ID number',false);return;}}
  if(!idPhoto){{showToast('Upload ID proof photo (front)',false);return;}}
  const btn=document.getElementById('subBtn');
  btn.disabled=true;btn.textContent='Processing...';
  const opt=document.getElementById('roomSel').selectedOptions[0];
  const secret=opt?.dataset?.secret||'';
  const ag=[];
  for(let i=2;i<=count;i++){{
    const n=document.getElementById('ag_n_'+i)?.value.trim();
    if(n)ag.push({{name:n,id_proof_type:document.getElementById('ag_t_'+i)?.value||'',
      id_proof_number:document.getElementById('ag_id_'+i)?.value.trim()||'',
      id_proof_photo:document.getElementById('ag_fu_'+i)?.value||''}});
  }}
  try{{
    const r=await fetch('/api/guest/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{slug:SLUG,room,secret,checkin_date:ci,checkout_date:co,
        name,phone,alternate_phone:document.getElementById('gAlt').value.trim(),
        guest_count:count,id_proof_type:idType,id_proof_number:idNum,
        id_proof_photo:idPhoto,id_proof_photo_back:document.getElementById('idBUrl').value,
        customer_gstin:(document.getElementById('gGstin')?document.getElementById('gGstin').value.trim().toUpperCase():''),
        additional_guests:ag}})}});
    const d=await r.json();
    if(d.success){{
      document.querySelector('.wrap').innerHTML=`<div class="card" style="text-align:center;padding:28px">
        <div style="font-size:48px;margin-bottom:12px">✅</div>
        <h2 style="color:var(--p);font-family:'Playfair Display',serif;margin-bottom:10px">Registration Complete!</h2>
        <p style="opacity:.75;margin-bottom:16px">Your check-in request has been submitted.</p>
        <div style="background:rgba(255,255,255,.06);border-radius:8px;padding:13px;text-align:left;font-size:13px;line-height:1.8">
          <div>🏨 Room: <b>${{room}}</b></div>
          <div>📅 Check-in: <b>${{ci}}</b></div>
          <div>📅 Checkout: <b>${{co}}</b></div>
          <div>🔖 Booking: <b style="font-family:monospace">${{d.booking_id}}</b></div>
        </div>
        <p style="margin-top:14px;opacity:.6;font-size:12px">Reception will approve your check-in on WhatsApp shortly. Keep your phone nearby. 🙏</p>
      </div>`;
    }}else{{showToast(d.error||'Registration failed',false);btn.disabled=false;btn.textContent='✅ Complete Registration';}}
  }}catch(e){{showToast('Network error',false);btn.disabled=false;btn.textContent='✅ Complete Registration';}}
}}
const urlP=new URLSearchParams(window.location.search);
const urlRoom=urlP.get('room');
if(urlRoom){{const opts=[...document.getElementById('roomSel').options];const m=opts.find(o=>o.value===urlRoom);if(m){{document.getElementById('roomSel').value=urlRoom;onRoom();}}}}

// ── Returning-guest auto-fill ─────────────────────────────────────
let _luTimer=null,_luLast='';
document.getElementById('gPhone').addEventListener('input', function(){{
  const p=this.value.trim().replace(/\\D/g,'');
  if(p.length<10||p===_luLast)return;
  clearTimeout(_luTimer);
  _luTimer=setTimeout(async()=>{{
    _luLast=p;
    try{{
      const r=await fetch('/api/guest/lookup?slug='+encodeURIComponent(SLUG)+'&phone='+encodeURIComponent(p));
      const d=await r.json();
      const wb=document.getElementById('welcomeBack');
      const ni=document.getElementById('gName');
      const it=document.getElementById('idType');
      if(d&&d.found){{
        if(!ni.value||ni.value.trim().length<2)ni.value=d.name||'';
        if(it&&!it.value&&d.id_proof_type){{
          for(const o of it.options){{ if(o.value===d.id_proof_type){{ it.value=d.id_proof_type; break; }} }}
        }}
        wb.textContent='👋 Welcome back, '+(d.name||'guest')+'! ('+(d.total_visits||1)+' previous visit'+((d.total_visits||1)>1?'s':'')+')';
        wb.style.display='block';
      }}else{{
        wb.style.display='none';
      }}
    }}catch(e){{ /* silent */ }}
  }}, 350);
}});
</script>"""
    return HTMLResponse(themed(hotel,"Guest Registration",body))


# ── MENU / SERVICE PAGE ───────────────────────────────────────────
@router.get("/menu/{slug}", response_class=HTMLResponse)
async def menu_page(slug: str, request: Request):
    hotel = await _active_hotel_or_block(slug)
    hid = hotel["id"]
    services = await db.get_services(hid)
    pri = safe_color(hotel.get("primary_color"), "#c8a84b")

    cats: dict = {}
    for s in services:
        cats.setdefault(s.get("category","Other"),[]).append(s)

    svc_html = ""
    for cat, items in cats.items():
        cat_html = html_escape(cat)
        svc_html += f'<div class="ctitle">🔹 {cat_html}</div>'
        for s in items:
            p = float(s.get("price",0))
            ps = f"₹{p:.0f}" if p>0 else "Free"
            sname_raw = s.get("service_name", "")
            sname = html_escape(sname_raw)
            desc = html_escape(s.get("description","") or "")
            # JSON-encode the name for use inside the onclick handler so a
            # service named  Foo'); evil(); //  cannot break out of the
            # function call.
            sname_js = json.dumps(sname_raw)
            desc_html = f'<div style="font-size:11px;opacity:.55;margin-top:2px">{desc}</div>' if desc else ''
            svc_html += f"""<div class="scard" onclick="reqSvc({sname_js},{p})">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div><b style="font-size:14px">{sname}</b>{desc_html}</div>
                <span style="font-weight:600;color:{pri};white-space:nowrap;margin-left:10px">{ps}</span>
              </div></div>"""

    body = f"""
<div class="card" style="text-align:center;padding:14px">
  <div id="gInfo" style="font-size:13px;opacity:.65">Enter your room to get started</div>
</div>
<div class="card" id="initCard">
  <div class="ct">🏨 Confirm Your Room</div>
  <label>Room Number</label><input type="text" id="roomInp" placeholder="e.g. 101" style="text-transform:uppercase">
  <label>Your WhatsApp Number</label><input type="tel" id="phoneInp" placeholder="91XXXXXXXXXX">
  <button class="btn" onclick="initGuest()" style="margin-top:11px">Continue →</button>
</div>
<div id="svcDiv" style="display:none">
  <div class="card">
    <div class="ct">🛎️ Available Services</div>
    <div style="font-size:12px;opacity:.55;margin-bottom:12px">⏰ Hours: {int(hotel.get('svc_open_hour',7) or 7)}AM – {int(hotel.get('svc_close_hour',23) or 23)}PM · Checkout: {html_escape(hotel.get('checkout_time_display','11:00 AM'))}</div>
    {svc_html or '<p style="opacity:.5;text-align:center;padding:16px">No services available.</p>'}
  </div>
</div>
<div id="billDiv" style="display:none" class="card">
  <div class="ct">💰 My Bill Summary</div>
  <div id="billList"></div>
  <div id="billTotal" style="font-weight:700;margin-top:8px;color:{pri}"></div>
</div>
<script>
let gRoom='',gPhone='',gBid='';
async function initGuest(){{
  gRoom=document.getElementById('roomInp').value.trim().toUpperCase();
  gPhone=document.getElementById('phoneInp').value.trim();
  if(!gRoom||!gPhone){{showToast('Enter room and phone',false);return;}}
  const r=await fetch('/api/guest/session?phone='+gPhone);
  const d=await r.json();
  if(d.found){{
    gBid=d.booking_id;
    document.getElementById('gInfo').textContent='Room '+gRoom+' · '+d.name;
    document.getElementById('initCard').style.display='none';
    document.getElementById('svcDiv').style.display='block';
    document.getElementById('billDiv').style.display='block';
    loadBill();
  }}else showToast('No active booking for this number',false);
}}
async function loadBill(){{
  if(!gBid)return;
  const r=await fetch('/api/guest/charges?booking_id='+gBid);
  const d=await r.json();
  if(d.charges&&d.charges.length){{
    let h='',tot=0;
    d.charges.forEach(c=>{{
      h+=`<div style="display:flex;justify-content:space-between;font-size:12px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.07)">
        <span>${{c.description}}</span><span>₹${{parseFloat(c.total).toFixed(0)}}</span></div>`;
      tot+=parseFloat(c.total);
    }});
    document.getElementById('billList').innerHTML=h;
    document.getElementById('billTotal').textContent='Total: ₹'+tot.toFixed(0);
  }}
}}
async function reqSvc(svc,price){{
  if(!gRoom||!gPhone){{showToast('Confirm your room first',false);return;}}
  if(price>0&&!confirm(svc+'\\n₹'+price+' will be added to your bill. Confirm?'))return;
  const r=await fetch('/api/guest/service',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{slug:{json.dumps(slug)},room:gRoom,phone:gPhone,booking_id:gBid,service:svc}})}});
  const d=await r.json();
  if(d.success){{showToast('✅ Request sent! Our team will attend shortly.');loadBill();}}
  else showToast('Error: '+(d.error||'Try again'),false);
}}
const p=new URLSearchParams(window.location.search);
if(p.get('r'))document.getElementById('roomInp').value=p.get('r');
if(p.get('p')){{document.getElementById('phoneInp').value=p.get('p');if(p.get('r'))setTimeout(initGuest,300);}}
</script>"""
    return HTMLResponse(themed(hotel,"Services & Menu",body))


# ── BILL PAGE ─────────────────────────────────────────────────────
@router.get("/bill/{slug}", response_class=HTMLResponse)
async def bill_page(slug: str, request: Request):
    hotel = await _active_hotel_or_block(slug)
    pri = safe_color(hotel.get("primary_color"), "#c8a84b")
    body = f"""
<div class="card"><div class="ct">💰 View Your Bill</div>
  <label>Your WhatsApp Number</label>
  <input type="tel" id="bPhone" placeholder="91XXXXXXXXXX">
  <button class="btn" onclick="loadBill()">View My Bill →</button>
</div>
<div id="bDiv" style="display:none"></div>
<script>
// Per-guest token delivered via the bot's WhatsApp URLs (/bill/{{slug}}?phone=...&t=...).
// The /api/guest/bill endpoint validates this against the Redis session
// before returning data, so a stranger who only knows a phone cannot
// pull the bill. Empty string for legacy in-stay sessions that predate
// the change — endpoint keeps falling back to phone+slug for those.
const URL_PARAMS=new URLSearchParams(window.location.search);
const GUEST_TOKEN=URL_PARAMS.get('t')||URL_PARAMS.get('token')||'';
async function loadBill(){{
  const phone=document.getElementById('bPhone').value.trim();
  if(!phone){{showToast('Enter your phone number',false);return;}}
  const r=await fetch('/api/guest/bill?phone='+encodeURIComponent(phone)+'&slug='+encodeURIComponent({json.dumps(slug)})+'&token='+encodeURIComponent(GUEST_TOKEN));
  const d=await r.json();
  if(!d.found){{showToast(d.error||'No active booking found',false);return;}}
  let h=`<div class="card"><div class="ct">🔖 Booking Details</div>
    <div style="font-size:13px;line-height:1.8">
      <div>👤 <b>${{d.guest_name}}</b></div>
      <div>🏨 Room: <b>${{d.room_number}}</b></div>
      <div>📅 ${{d.checkin_date}} → ${{d.checkout_date}}</div>
      <div style="font-family:monospace;font-size:11px">🔖 ${{d.booking_id}}</div>
    </div></div>
    <div class="card"><div class="ct">📋 Charges</div>`;
  let tot=0,paid=0;
  (d.charges||[]).forEach(c=>{{
    const t=parseFloat(c.total);tot+=t;
    if(c.payment_status==='Paid')paid+=t;
    const clr=c.payment_status==='Paid'?'#3fb950':'#f85149';
    h+=`<div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:13px">
      <div><div>${{c.description}}</div><div style="font-size:11px;opacity:.5">${{c.service_type}}</div></div>
      <div style="text-align:right"><div>₹${{t.toFixed(0)}}</div><div style="font-size:11px;color:${{clr}}">${{c.payment_status}}</div></div>
    </div>`;
  }});
  const bal=tot-paid;
  h+=`<div style="margin-top:12px;padding-top:10px;border-top:2px solid rgba(255,255,255,.12)">
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px"><span>Total Billed</span><span>₹${{tot.toFixed(0)}}</span></div>
    <div style="display:flex;justify-content:space-between;font-size:13px;color:#3fb950;margin-bottom:4px"><span>Paid</span><span>₹${{paid.toFixed(0)}}</span></div>
    <div style="display:flex;justify-content:space-between;font-size:15px;font-weight:700;color:${{bal>0?'#f85149':'#3fb950'}}">
      <span>Balance Due</span><span>${{bal>0?'₹'+bal.toFixed(0):'✓ PAID'}}</span></div>
  </div></div>`;
  document.getElementById('bDiv').innerHTML=h;
  document.getElementById('bDiv').style.display='block';
}}
const p=new URLSearchParams(window.location.search);
if(p.get('phone')){{document.getElementById('bPhone').value=p.get('phone');loadBill();}}
</script>"""
    return HTMLResponse(themed(hotel,"My Bill",body))


# ── GUEST API ENDPOINTS ───────────────────────────────────────────
@router.post("/api/guest/register")
async def api_register(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"success":False,"error":"Invalid JSON"},400)
    slug = body.get("slug","")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: return JSONResponse({"success":False,"error":"Hotel not found"},404)
    if not hotel.get("is_active"):
        # Don't even let a guest who already loaded the form (perhaps before
        # the pause kicked in) submit a new booking. We reuse the friendly
        # 503 message format the page-route helper produces.
        until = hotel.get("paused_until")
        msg = f"{hotel.get('hotel_name','This hotel')} is temporarily not accepting new bookings."
        if until:
            try:
                from datetime import timedelta
                ist = until + timedelta(hours=5, minutes=30)
                msg += f" Reopens around {ist.strftime('%d %b %Y, %H:%M IST')}."
            except Exception:
                pass
        return JSONResponse({"success": False, "error": msg}, 503)
    hid = hotel["id"]
    room  = str(body.get("room","")).strip().upper()
    secret= str(body.get("secret","")).strip()
    phone = str(body.get("phone","")).strip()
    name  = str(body.get("name","")).strip()
    ci    = str(body.get("checkin_date","")).strip()
    co    = str(body.get("checkout_date","")).strip()
    count = int(body.get("guest_count",1))
    alt   = str(body.get("alternate_phone","")).strip()
    extra = body.get("additional_guests",[])
    idt   = str(body.get("id_proof_type","")).strip()
    idn   = str(body.get("id_proof_number","")).strip().upper()
    idp   = str(body.get("id_proof_photo","")).strip()
    idb   = str(body.get("id_proof_photo_back","")).strip()
    gstin = str(body.get("customer_gstin","")).strip().upper()
    # Foreign-guest fields (Form C / FRRO). All optional; only the foreign flag
    # plus passport/visa fields trigger the FormC-pending workflow.
    is_foreign = bool(body.get("is_foreign_guest", False))
    nationality = str(body.get("nationality","")).strip()
    sex = str(body.get("sex","")).strip()
    dob = str(body.get("date_of_birth","")).strip()
    passport_no = str(body.get("passport_no","")).strip().upper()
    passport_place = str(body.get("passport_place_of_issue","")).strip()
    passport_issue = str(body.get("passport_issue_date","")).strip()
    passport_expiry = str(body.get("passport_expiry_date","")).strip()
    visa_no = str(body.get("visa_no","")).strip().upper()
    visa_type = str(body.get("visa_type","")).strip()
    visa_place = str(body.get("visa_issue_place","")).strip()
    visa_issue = str(body.get("visa_issue_date","")).strip()
    visa_expiry = str(body.get("visa_expiry_date","")).strip()
    arrival_date = str(body.get("arrival_in_india_date","")).strip()
    arrival_port = str(body.get("arrival_in_india_port","")).strip()
    last_country = str(body.get("last_country_visited","")).strip()
    next_dest = str(body.get("next_destination","")).strip()
    purpose = str(body.get("purpose_of_visit","")).strip()
    if not all([room,phone,name,ci,co]):
        return JSONResponse({"success":False,"error":"Missing required fields"},400)
    room_row = await db.get_room(room, hid)
    if not room_row: return JSONResponse({"success":False,"error":f"Room {room} not found"},400)
    # Fail CLOSED on the QR secret. The previous form
    #     `if room_row.get("qr_secret") and room_row["qr_secret"] != secret`
    # silently allowed registrations against any room whose qr_secret was
    # empty (e.g. a freshly-created room before the operator generated a
    # QR), turning the "scan the QR in your room" flow into "anyone with
    # the slug can self-register into that room". Operators must now
    # generate a QR (which writes a random secret) before guests can
    # register against the room.
    expected_secret = (room_row.get("qr_secret") or "").strip()
    submitted_secret = (secret or "").strip()
    if not expected_secret or not submitted_secret or not hmac.compare_digest(
        expected_secret.encode("utf-8"), submitted_secret.encode("utf-8")
    ):
        return JSONResponse({"success":False,"error":"Invalid QR code. Scan the QR inside your room."},400)
    occ = await cache_get_room(room)
    if occ and occ.replace("PENDING:","") != phone:
        return JSONResponse({"success":False,"error":f"Room {room} is currently occupied."},400)
    bk_id = gen_bk()
    ttl = calc_ttl(co)
    nights = calc_nights(ci, co)
    rate = float(room_row.get("room_rate",0) or 0)
    instance = hotel["instance_name"]
    staff_phones = await db.get_staff_phones(hid)
    # Guest session token. The bot includes this in every URL it sends to
    # the guest's WhatsApp (food/bill/services). The /api/guest/charges,
    # /api/guest/bill, /api/guest/food/my-orders endpoints validate it
    # before returning data — so a stranger who just *knows* a phone (e.g.
    # via /api/guest/lookup) can no longer pull that guest's bill.
    guest_token = secrets.token_urlsafe(24)
    sess = {"phone":phone,"name":name,"room":room,"bookingId":bk_id,
            "checkinDate":ci,"checkoutDate":co,"status":"AWAITING_APPROVAL",
            "orders":[],"sessionType":"HOTEL","hotelId":hid,
            "hotelName":hotel["hotel_name"],"createdAt":ist_now().isoformat(),"TTL":ttl,
            "guest_token":guest_token}
    await set_session(phone, sess, ttl)
    await set_room(room, f"PENDING:{phone}", ttl)
    await db.insert_booking({"booking_id":bk_id,"room_number":room,"guest_name":name,
        "guest_phone":phone,"checkin_date":ci,"checkout_date":co,
        "payment_mode":"Pay at checkout","id_proof_type":idt,"id_proof_number":idn,
        "id_proof_photo":idp,"id_proof_photo_back":idb,"guest_count":count,
        "alternate_phone":alt,"hotel_id":hid,"customer_gstin":gstin,
        "is_foreign_guest":is_foreign,"nationality":nationality,
        "sex":sex,"date_of_birth":dob,
        "passport_no":passport_no,"passport_place_of_issue":passport_place,
        "passport_issue_date":passport_issue,"passport_expiry_date":passport_expiry,
        "visa_no":visa_no,"visa_type":visa_type,"visa_issue_place":visa_place,
        "visa_issue_date":visa_issue,"visa_expiry_date":visa_expiry,
        "arrival_in_india_date":arrival_date,"arrival_in_india_port":arrival_port,
        "last_country_visited":last_country,"next_destination":next_dest,
        "purpose_of_visit":purpose})
    if extra: await db.insert_additional_guests(bk_id, extra, hid)
    if rate > 0:
        await db.insert_stay_charge({"booking_id":bk_id,"charge_date":date.today(),
            "service_type":"Room Rent","description":f"Room {room} — {nights} night(s)",
            "amount":rate*nights,"total":rate*nights,"payment_status":"Pending","hotel_id":hid})
    guests_txt = f"👤 {name} | {idt}: {idn}\n"
    for i,ag in enumerate(extra):
        guests_txt += f"👤 Guest {i+2}: {ag.get('name','')} | {ag.get('id_proof_type','')}: {ag.get('id_proof_number','')}\n"
    from config.settings import BASE_URL
    await send_to_phones(instance, staff_phones,
        f"🔔 *NEW CHECK-IN REQUEST*\n━━━━━━━━━━━━━━━━━━\n"
        f"🏨 {hotel['hotel_name']}\n🛏️ Room: *{room}*\n👥 Guests: {count}\n"
        f"🔖 {bk_id}\n📅 {ci} → {co}\n📱 {phone}\n\n{guests_txt}"
        f"━━━━━━━━━━━━━━━━━━\n✅ APPROVE {phone}\n❌ REJECT {phone}\n\n"
        f"📊 Dashboard: {BASE_URL}/hotel/{slug}")
    await send_text(instance, phone,
        f"🏨 *Welcome to {hotel['hotel_name']}!*\n\n"
        f"🙏 Namaste {name.split()[0]}!\n\nRoom *{room}* registration received.\n"
        f"Booking ID: `{bk_id}`\n\n⏳ Waiting for reception approval. Keep your phone nearby! 🙏")
    return JSONResponse({"success":True,"booking_id":bk_id})

@router.get("/api/guest/session")
async def api_session(phone: str = ""):
    sess = await get_session(phone)
    if not sess: return JSONResponse({"found":False})
    return JSONResponse({"found":True,"name":sess.get("name"),"room":sess.get("room"),"booking_id":sess.get("bookingId"),"status":sess.get("status")})


@router.get("/api/guest/lookup")
async def api_lookup(request: Request, slug: str = "", phone: str = ""):
    """
    Returning-guest auto-fill. If this phone has stayed at the hotel before,
    return name + ID-type so the registration form can pre-fill them.
    Always returns {found,...} — never errors out.
    Sensitive fields (ID number, photos) are NOT returned — guest re-enters every visit.

    SECURITY: This endpoint is unauthenticated by design (the registration
    page calls it before the guest is registered). To stop a runaway
    scraper from enumerating the hotel's phone book, we apply two
    independent rate-limit buckets:
      * per source IP   — 30 lookups / minute
      * per slug+phone  — 10 lookups / 5 minutes  (catches distributed
        scrapers spreading guesses across many IPs against a single
        target phone)
    Both caps fail open if Redis is unreachable.
    """
    s = (slug or "").strip()
    p = (phone or "").strip()
    # Validate phone shape early so we don't even rate-limit obvious junk.
    if not s or not p or len(p) < 10 or len(p) > 14 or not p.isdigit():
        return JSONResponse({"found": False})

    ip = _client_ip(request)
    if not await rate_limit_check(f"lookup:ip:{s}:{ip}", limit=30, window_seconds=60):
        return JSONResponse(
            {"found": False, "error": "Too many requests, please slow down."},
            status_code=429,
        )
    if not await rate_limit_check(f"lookup:phone:{s}:{p}", limit=10, window_seconds=300):
        # Don't leak which axis tripped — return the same 429 shape as the
        # IP cap above. Logged at info so operators can spot enumeration.
        logger.info("guest lookup throttled per slug+phone slug=%s phone=%s", s, p)
        return JSONResponse(
            {"found": False, "error": "Too many requests, please slow down."},
            status_code=429,
        )

    try:
        hotel = await db.get_hotel_by_slug(s)
        if not hotel:
            return JSONResponse({"found": False})
        info = await db.lookup_returning_guest(hotel["id"], p)
        return JSONResponse(info or {"found": False})
    except Exception:
        return JSONResponse({"found": False})

@router.get("/api/guest/charges")
async def api_charges(phone: str = "", slug: str = "", booking_id: str = "", token: str = ""):
    """
    Return the charges for the caller's CURRENT active booking.

    SECURITY: Bound to the per-guest session token (`guest_token`) minted
    at registration time and delivered to the guest only via the bot's
    WhatsApp URLs. A caller who merely *knows* a phone number (e.g. via
    /api/guest/lookup) but never received the URL can no longer pull
    that guest's charges.

    Sessions created BEFORE this PR don't carry a token; we fall back to
    the prior phone+slug+active-booking behaviour for them so existing
    in-stay guests don't get locked out. New sessions strictly require
    the token. After a single TTL window (≤24h) every active session
    will be on the new path.
    """
    if not phone or not slug:
        return JSONResponse({"charges": []})
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"charges": []})
    state, _ = await _get_guest_token_session(phone.strip(), token)
    if state == "bad":
        return JSONResponse({"charges": [], "error": "Invalid session"}, status_code=403)
    bk = await db.get_active_booking_by_phone(phone.strip(), hotel["id"])
    if not bk:
        return JSONResponse({"charges": []})
    if booking_id and booking_id != bk["booking_id"]:
        # Caller is asking about someone else's booking; deny silently.
        return JSONResponse({"charges": []})
    charges = await db.get_charges_for_booking(bk["booking_id"], hotel_id=hotel["id"])
    return JSONResponse({"charges": charges})

@router.get("/api/guest/bill")
async def api_bill(phone: str = "", slug: str = "", token: str = ""):
    """
    Show the bill for the caller's CURRENT active booking.

    SECURITY: Same session-token binding as /api/guest/charges. The
    `phone` + `slug` pair on its own is no longer sufficient — the
    caller must also present the `guest_token` minted at registration
    and delivered via the bot's WhatsApp URLs. Pre-PR sessions still
    work (legacy fallback) until they expire naturally.
    """
    if not phone or not slug:
        return JSONResponse({"found": False})
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: return JSONResponse({"found":False})
    state, _ = await _get_guest_token_session(phone.strip(), token)
    if state == "bad":
        return JSONResponse({"found": False, "error": "Invalid session"}, status_code=403)
    bk = await db.get_active_booking_by_phone(phone.strip(), hotel["id"])
    if not bk: return JSONResponse({"found":False})
    charges = await db.get_charges_for_booking(bk["booking_id"], hotel_id=hotel["id"])
    return JSONResponse({"found":True,"guest_name":bk["guest_name"],"room_number":bk["room_number"],
        "booking_id":bk["booking_id"],"checkin_date":fmt_date(bk.get("checkin_date")),
        "checkout_date":fmt_date(bk.get("checkout_date")),"charges":charges})

@router.post("/api/guest/service")
async def api_service(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"success":False},400)
    slug = body.get("slug","")
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: return JSONResponse({"success":False,"error":"Hotel not found"},404)
    hid = hotel["id"]
    phone = str(body.get("phone","")).strip()
    room  = str(body.get("room","")).strip()
    svc   = str(body.get("service","")).strip()
    bid   = str(body.get("booking_id","")).strip()
    cat, dept = categorize_service(svc)
    sr_id = gen_sr()
    price = await db.fetchval("SELECT price FROM services WHERE hotel_id=$1 AND LOWER(service_name)=LOWER($2) AND is_active=TRUE LIMIT 1",hid,svc) or 0
    price = float(price)
    await db.insert_service_request({"request_id":sr_id,"phone":phone,"booking_id":bid,"service_name":svc[:100],"category":cat,"department":dept,"price":price})
    if price > 0 and bid:
        await db.insert_stay_charge({"booking_id":bid,"charge_date":date.today(),"service_type":cat,"description":svc[:100],"amount":price,"total":price,"payment_status":"Pending","order_ref":sr_id,"hotel_id":hid})
    dept_phone = await db.get_dept_phone(dept, hid)
    staff_phones = await db.get_staff_phones(hid)
    notify = [dept_phone] if dept_phone else staff_phones
    await send_to_phones(hotel["instance_name"], notify,
        f"🛎️ *SERVICE REQUEST*\n━━━━━━━━━━━━━━━━━━\n🏨 Room: *{room}*\n🔖 {sr_id}\n📋 *{svc}*\n{'💰 ₹'+str(int(price)) if price>0 else ''}\n\nReply *DONE {sr_id}* when done.")
    await send_text(hotel["instance_name"], phone,
        f"✅ *Request Received!*\n🛎️ {svc[:60]}\n🔖 {sr_id}\n\nOur team will attend shortly. 🙏")
    return JSONResponse({"success":True,"request_id":sr_id})




# ══════════════════════════════════════════════════════════════════
# REAL FOOD / RESTAURANT — guest-facing page + APIs
# /food/{slug}                    → mobile-first menu page (themed)
# GET  /api/guest/food/menu       → menu JSON (categories + items)
# POST /api/guest/food/order      → place an order (creates stay_charge)
# GET  /api/guest/food/my-orders  → live status of guest's recent orders
# ══════════════════════════════════════════════════════════════════
@router.get("/food/{slug}", response_class=HTMLResponse)
async def food_page(slug: str, request: Request):
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        raise HTTPException(404)
    pri = safe_color(hotel.get("primary_color"), "#c8a84b")
    body = f"""
<div class="card" id="initCard">
  <div class="ct">🍽️ Order Food to Your Room</div>
  <p style="font-size:13px;opacity:.65;margin-bottom:12px">Enter your room and WhatsApp number to start.</p>
  <label>Room Number</label>
  <input type="text" id="roomInp" placeholder="e.g. 101" style="text-transform:uppercase">
  <label>Your WhatsApp Number</label>
  <input type="tel" id="phoneInp" placeholder="91XXXXXXXXXX">
  <button class="btn" onclick="initGuest()" style="margin-top:11px">View Menu →</button>
</div>

<div id="menuCard" style="display:none">
  <div class="card" id="gInfoCard" style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:13px;color:{pri};font-weight:600" id="gInfo"></div>
      <div style="font-size:11px;opacity:.55" id="gSub"></div>
    </div>
    <div class="btn" style="width:auto;padding:8px 14px;font-size:13px;margin-top:0" onclick="openCart()">
      🛒 Cart <span id="cartCount">0</span>
    </div>
  </div>

  <div class="card">
    <div class="ct">Filter</div>
    <input type="text" id="searchInp" placeholder="Search dishes..." oninput="renderMenu()" style="margin-bottom:8px">
    <div id="catBar" style="display:flex;gap:6px;flex-wrap:wrap"></div>
  </div>

  <div id="menuList"></div>
</div>

<!-- CART DRAWER -->
<div id="cartOverlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200" onclick="closeCart()"></div>
<div id="cartDrawer" style="display:none;position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:2px solid {pri};border-radius:20px 20px 0 0;z-index:201;max-height:80vh;overflow-y:auto;padding:18px;color:var(--t)">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div style="font-size:16px;font-weight:700;color:{pri}">🛒 Your Order</div>
    <div onclick="closeCart()" style="cursor:pointer;font-size:22px;opacity:.6">×</div>
  </div>
  <div id="cartItems"></div>
  <textarea id="orderNotes" placeholder="Special instructions (optional)..." style="width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:var(--t);padding:9px 11px;border-radius:7px;font-size:13px;margin-top:10px;font-family:inherit;min-height:60px;resize:none"></textarea>
  <div id="cartTotal" style="font-weight:700;margin-top:12px;color:{pri};text-align:right;font-size:15px"></div>
  <button class="btn" onclick="placeOrder()" id="placeBtn" style="margin-top:10px">📤 Place Order</button>
</div>

<!-- MY ORDERS -->
<div id="myOrders" style="display:none;margin-top:16px"></div>

<script>
const SLUG={json.dumps(slug)};
// Per-guest session token delivered by the bot via WhatsApp URLs
// (/food/{{slug}}?r=...&p=...&t=...). Validated by /api/guest/food/my-orders
// against the Redis session. Empty string falls back to legacy behaviour
// for in-stay guests whose sessions predate this change.
const URL_PARAMS=new URLSearchParams(window.location.search);
const GUEST_TOKEN=URL_PARAMS.get('t')||URL_PARAMS.get('token')||'';
let MENU=[],CATS=[],activeCat='All',gRoom='',gPhone='',gName='',gBid='';
let cart={{}};

async function initGuest(){{
  gRoom=document.getElementById('roomInp').value.trim().toUpperCase();
  gPhone=document.getElementById('phoneInp').value.trim();
  if(!gRoom||!gPhone){{showToast('Enter room and phone',false);return;}}
  const r=await fetch('/api/guest/session?phone='+gPhone);
  const d=await r.json();
  if(!d.found){{showToast('No active booking for this number. Please register first.',false);return;}}
  gName=d.name||'Guest';
  gBid=d.booking_id||'';
  document.getElementById('gInfo').textContent=`Room ${{gRoom}} · ${{gName}}`;
  document.getElementById('gSub').textContent='Tap any dish to add it to your order';
  document.getElementById('initCard').style.display='none';
  document.getElementById('menuCard').style.display='block';
  await loadMenu();
  await loadMyOrders();
}}

async function loadMenu(){{
  const r=await fetch('/api/guest/food/menu?slug='+SLUG);
  const d=await r.json();
  if(!d.items){{ document.getElementById('menuList').innerHTML='<div class="card"><p style="opacity:.5;text-align:center">No menu yet — please ask reception.</p></div>'; return; }}
  MENU=d.items; CATS=['All', ...(d.categories||[])];
  renderCats();
  renderMenu();
}}

function renderCats(){{
  const bar=document.getElementById('catBar');
  bar.innerHTML=CATS.map(c=>`<span style="padding:5px 12px;border-radius:14px;font-size:11px;cursor:pointer;border:1px solid ${{c===activeCat?'var(--p)':'rgba(255,255,255,.15)'}};background:${{c===activeCat?'rgba(200,168,75,.18)':'transparent'}};color:${{c===activeCat?'var(--p)':'rgba(255,255,255,.65)'}}" onclick="filterCat('${{c.replace(/'/g,"\\\\'")}}')">${{c}}</span>`).join('');
}}
function filterCat(c){{ activeCat=c; renderCats(); renderMenu(); }}

function renderMenu(){{
  const q=(document.getElementById('searchInp').value||'').toLowerCase();
  const filtered=MENU.filter(m=>(activeCat==='All'||m.category===activeCat) &&
                                  (m.name.toLowerCase().includes(q) || (m.description||'').toLowerCase().includes(q)));
  if(!filtered.length){{ document.getElementById('menuList').innerHTML='<div class="card"><p style="opacity:.5;text-align:center">No dishes match.</p></div>'; return; }}
  // group by category
  const groups={{}};
  filtered.forEach(m=>{{ (groups[m.category]=groups[m.category]||[]).push(m); }});
  let h='';
  Object.keys(groups).forEach(cat=>{{
    h+=`<div class="ctitle">🍽 ${{cat}}</div>`;
    groups[cat].forEach(m=>{{
      const dot = m.type==='nonveg' ? '🔴' : (m.type==='egg' ? '🟡' : '🟢');
      const star= m.is_bestseller ? '<span style="color:#ffd166;font-size:10px;margin-left:4px">⭐ Best</span>' : '';
      const dis = !m.is_available ? 'opacity:.4;pointer-events:none' : '';
      const inCart = cart[m.id] ? cart[m.id].qty : 0;
      const qtyHtml = inCart>0
        ? `<div style="display:flex;align-items:center;gap:8px"><button class="qb" onclick="changeQty(${{m.id}},-1)">−</button><span style="min-width:18px;text-align:center;font-weight:600">${{inCart}}</span><button class="qb" onclick="changeQty(${{m.id}},1)">+</button></div>`
        : `<button class="addb" onclick="changeQty(${{m.id}},1)">+ Add</button>`;
      const img = m.image_url ? `<img src="${{m.image_url}}" style="width:54px;height:54px;border-radius:8px;object-fit:cover;flex-shrink:0">` : '';
      h+=`<div class="scard" style="${{dis}};display:flex;justify-content:space-between;align-items:center;gap:11px">
        ${{img}}
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:5px"><b style="font-size:14px">${{dot}} ${{m.name}}</b>${{star}}</div>
          ${{m.description ? `<div style="font-size:11px;opacity:.55;margin-top:2px">${{m.description}}</div>` : ''}}
          <div style="font-weight:600;color:var(--p);margin-top:4px">₹${{Math.round(m.price)}}</div>
        </div>
        <div>${{!m.is_available ? '<span style="opacity:.5;font-size:11px">Unavailable</span>' : qtyHtml}}</div>
      </div>`;
    }});
  }});
  document.getElementById('menuList').innerHTML=h;
}}

function changeQty(id,delta){{
  const item=MENU.find(m=>m.id===id);
  if(!item)return;
  const cur=cart[id]?cart[id].qty:0;
  const next=Math.max(0,cur+delta);
  if(next===0) delete cart[id];
  else cart[id]={{item,qty:next}};
  renderMenu();
  updateCartBadge();
}}

function updateCartBadge(){{
  const n=Object.values(cart).reduce((s,c)=>s+c.qty,0);
  document.getElementById('cartCount').textContent=n;
  if(document.getElementById('cartDrawer').style.display==='block') renderCart();
}}

function openCart(){{
  document.getElementById('cartOverlay').style.display='block';
  document.getElementById('cartDrawer').style.display='block';
  renderCart();
}}
function closeCart(){{
  document.getElementById('cartOverlay').style.display='none';
  document.getElementById('cartDrawer').style.display='none';
}}

function renderCart(){{
  const items=Object.values(cart);
  const list=document.getElementById('cartItems');
  if(!items.length){{
    list.innerHTML='<p style="opacity:.5;text-align:center;padding:18px">Cart is empty</p>';
    document.getElementById('cartTotal').textContent='';
    document.getElementById('placeBtn').disabled=true;
    return;
  }}
  let h='',total=0;
  items.forEach(c=>{{
    const lt=c.item.price*c.qty; total+=lt;
    h+=`<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:13px">
      <div><div>${{c.item.name}}</div><div style="font-size:11px;opacity:.5">₹${{Math.round(c.item.price)}} each</div></div>
      <div style="display:flex;align-items:center;gap:9px">
        <button class="qb" onclick="changeQty(${{c.item.id}},-1)">−</button>
        <span style="min-width:18px;text-align:center;font-weight:600">${{c.qty}}</span>
        <button class="qb" onclick="changeQty(${{c.item.id}},1)">+</button>
        <span style="font-weight:600;min-width:50px;text-align:right">₹${{Math.round(lt)}}</span>
      </div>
    </div>`;
  }});
  list.innerHTML=h;
  document.getElementById('cartTotal').textContent='Total: ₹'+Math.round(total);
  document.getElementById('placeBtn').disabled=false;
}}

async function placeOrder(){{
  const items=Object.values(cart).map(c=>({{food_item_id:c.item.id,qty:c.qty}}));
  if(!items.length) return;
  const btn=document.getElementById('placeBtn');
  btn.disabled=true; btn.textContent='Placing...';
  try{{
    const r=await fetch('/api/guest/food/order',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{slug:SLUG, room:gRoom, phone:gPhone, booking_id:gBid,
        items, notes: document.getElementById('orderNotes').value.trim()}})}});
    const d=await r.json();
    if(d.success){{
      cart={{}};
      document.getElementById('orderNotes').value='';
      updateCartBadge();
      closeCart();
      showToast('✅ Order placed! Reception will confirm.');
      renderMenu();
      loadMyOrders();
    }}else{{
      showToast('❌ '+(d.error||'Failed'),false);
      btn.disabled=false; btn.textContent='📤 Place Order';
    }}
  }}catch(e){{
    showToast('Network error',false);
    btn.disabled=false; btn.textContent='📤 Place Order';
  }}
}}

async function loadMyOrders(){{
  if(!gPhone) return;
  const r=await fetch('/api/guest/food/my-orders?slug='+SLUG+'&phone='+gPhone+'&token='+encodeURIComponent(GUEST_TOKEN));
  const d=await r.json();
  const wrap=document.getElementById('myOrders');
  if(!d.orders||!d.orders.length){{ wrap.style.display='none'; return; }}
  let h='<div class="card"><div class="ct">📋 Your Orders</div>';
  d.orders.forEach(o=>{{
    const items=(o.items_json||[]).map(it=>`${{it.qty}}x ${{it.name}}`).join(', ');
    const colors={{Placed:'#7d8590',Preparing:'#d29922',Ready:'#3fb950',Delivered:'#3fb950',Cancelled:'#f85149'}};
    h+=`<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:13px">
      <div style="display:flex;justify-content:space-between"><b>#${{o.id}}</b>
        <span style="color:${{colors[o.status]||'#aaa'}};font-size:11px;font-weight:600">● ${{o.status}}</span></div>
      <div style="opacity:.65;font-size:11px;margin:2px 0">${{items}}</div>
      <div style="text-align:right;color:var(--p);font-weight:600">₹${{Math.round(o.total)}}</div>
    </div>`;
  }});
  h+='</div>';
  wrap.innerHTML=h;
  wrap.style.display='block';
}}

const urlP=new URLSearchParams(window.location.search);
if(urlP.get('r')) document.getElementById('roomInp').value=urlP.get('r');
if(urlP.get('p')) {{
  document.getElementById('phoneInp').value=urlP.get('p');
  if(urlP.get('r')) setTimeout(initGuest,200);
}}
// Auto-refresh "my orders" every 20s so guests see "Ready"/"Delivered" updates
setInterval(()=>{{ if(gPhone) loadMyOrders(); }}, 20000);
</script>
<style>
.qb {{ width:26px; height:26px; border-radius:50%; border:1px solid var(--p); background:transparent; color:var(--p); font-size:14px; cursor:pointer; line-height:1; }}
.qb:hover {{ background:var(--p); color:#000; }}
.addb {{ background:var(--btn); color:#000; font-size:12px; font-weight:600; padding:6px 12px; border-radius:7px; border:none; cursor:pointer; }}
</style>
"""
    return HTMLResponse(themed(hotel, "Order Food", body))


@router.get("/api/guest/food/menu")
async def api_food_menu(slug: str = ""):
    """Public food menu for a hotel — only available items, no admin fields."""
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"items": [], "categories": []})
    items = await db.list_food_items(hotel["id"], available_only=True)
    cats  = await db.list_food_categories(hotel["id"])
    # Trim to public-safe fields
    public = [{
        "id":            it["id"],
        "category":      it["category"],
        "name":          it["name"],
        "description":   it["description"] or "",
        "price":         float(it["price"] or 0),
        "image_url":     it["image_url"] or "",
        "type":          (it["type"] or "veg").lower(),
        "is_available":  bool(it["is_available"]),
        "is_bestseller": bool(it["is_bestseller"]),
        "spice_level":   it.get("spice_level") or "",
    } for it in items]
    return JSONResponse({"items": public, "categories": cats})


@router.post("/api/guest/food/order")
async def api_food_order(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, 400)

    slug   = body.get("slug", "")
    hotel  = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, 404)

    phone  = (body.get("phone") or "").strip()
    room   = (body.get("room") or "").strip().upper()
    bid    = (body.get("booking_id") or "").strip()
    items  = body.get("items") or []
    notes  = (body.get("notes") or "").strip()

    if not phone or not items:
        return JSONResponse({"success": False, "error": "phone and items are required"}, 400)

    # If caller didn't pass a booking_id, look it up from the active session
    # so the order links back to the room bill.
    name = ""
    if not bid or not room:
        booking = await db.get_active_booking_by_phone(phone, hotel["id"])
        if booking:
            bid  = bid  or booking["booking_id"]
            room = room or booking["room_number"]
            name = booking["guest_name"]

    try:
        order = await db.create_food_order(hotel["id"], {
            "booking_id":  bid,
            "room_number": room,
            "guest_phone": phone,
            "guest_name":  name,
            "items":       items,
            "notes":       notes,
        })
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, 400)

    # Notify kitchen / staff
    try:
        from services.whatsapp import send_to_phones, send_text
        staff_phones = await db.get_staff_phones(hotel["id"])
        item_lines = "\n".join(
            f"  • {s['qty']}x {s['name']} (₹{int(s['price'])})"
            for s in order.get("items", [])
        )
        notes_line = f"\n📝 {notes}" if notes else ""
        await send_to_phones(hotel["instance_name"], staff_phones,
            f"🍽️ *NEW FOOD ORDER #{order['id']}*\n━━━━━━━━━━━━━━━━━━\n"
            f"🏨 Room: *{room}*\n👤 {name or phone}\n📱 {phone}\n\n"
            f"{item_lines}{notes_line}\n\n"
            f"💰 ₹{float(order['total']):.0f}\n\n"
            f"✅ When ready: *FOOD READY R{room}* (or update from dashboard)"
        )
        await send_text(hotel["instance_name"], phone,
            f"✅ *Order Received!*\n🍽️ Room {room}\n\n{item_lines}\n\n"
            f"💰 ₹{float(order['total']):.0f} added to your bill.\n\n"
            f"We'll notify you when it's ready! 🙏")
    except Exception:
        pass  # never fail the order over notification issues

    return JSONResponse({
        "success":  True,
        "order_id": order["id"],
        "total":    float(order["total"]),
        "items":    order.get("items", []),
        "status":   order["status"],
    })


@router.get("/api/guest/food/my-orders")
async def api_food_my_orders(slug: str = "", phone: str = "", token: str = ""):
    """
    SECURITY: Bound to the per-guest session token (`guest_token`).
    See /api/guest/charges and /api/guest/bill for the full rationale.
    Sessions created before this PR (no token) still work via legacy
    phone+slug lookup; new sessions strictly require the token.
    """
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel or not phone:
        return JSONResponse({"orders": []})
    state, _ = await _get_guest_token_session(phone.strip(), token)
    if state == "bad":
        return JSONResponse({"orders": [], "error": "Invalid session"}, status_code=403)
    rows = await db.fetch(
        "SELECT id, status, items_json, subtotal, tax, total, notes, created_at, delivered_at "
        "FROM hotel_food_orders WHERE hotel_id=$1 AND guest_phone=$2 "
        "ORDER BY id DESC LIMIT 10",
        hotel["id"], phone,
    )
    out = []
    for r in rows:
        d = dict(r)
        # JSONB → already a python list of dicts via asyncpg
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        if d.get("delivered_at"):
            d["delivered_at"] = d["delivered_at"].isoformat()
        d["subtotal"] = float(d.get("subtotal") or 0)
        d["tax"]      = float(d.get("tax") or 0)
        d["total"]    = float(d.get("total") or 0)
        out.append(d)
    return JSONResponse({"orders": out})


# ══════════════════════════════════════════════════════════════════
# ONLINE PAYMENT PAGE + APIs
# /pay/{slug}                        -> branded payment page
# GET  /api/guest/payment-info       -> balance + payment methods
# POST /api/guest/create-razorpay-order  -> create Razorpay order
# POST /api/guest/verify-razorpay-payment -> verify + record payment
# ══════════════════════════════════════════════════════════════════

@router.get("/pay/{slug}", response_class=HTMLResponse)
async def payment_page(slug: str, request: Request):
    hotel = await _active_hotel_or_block(slug)
    pri = safe_color(hotel.get("primary_color"), "#c8a84b")
    body = f"""
<div class="card"><div class="ct">💳 Online Payment</div>
  <p style="font-size:13px;opacity:.65">Loading payment details...</p>
  <div id="loading" style="text-align:center;padding:20px;font-size:13px;opacity:.6">Please wait...</div>
</div>
<div id="paymentContent" style="display:none">
  <div class="card" id="billSummary">
    <div class="ct">📋 Bill Summary</div>
    <div id="chargesList"></div>
    <div id="balanceDue" style="font-weight:700;margin-top:12px;color:{pri};font-size:16px"></div>
  </div>
  <div id="razorpaySection" style="display:none" class="card">
    <div class="ct">💳 Pay Online (Razorpay)</div>
    <p style="font-size:12px;opacity:.6;margin-bottom:10px">Secure payment via Razorpay. Cards, UPI, Net Banking accepted.</p>
    <button class="btn" id="rzpBtn" onclick="initiateRazorpay()">Pay Online</button>
  </div>
  <div id="upiSection" style="display:none" class="card">
    <div class="ct">📱 Pay via UPI QR</div>
    <p style="font-size:12px;opacity:.6;margin-bottom:10px">Scan with any UPI app (GPay, PhonePe, Paytm). Staff will confirm receipt.</p>
    <div style="text-align:center"><img id="upiQrImg" style="max-width:280px;border-radius:10px;margin:10px auto"></div>
  </div>
  <div id="paidSection" style="display:none" class="card">
    <div style="font-size:48px;margin-bottom:12px;text-align:center">✅</div>
    <h3 style="color:{pri};text-align:center">Payment Confirmed!</h3>
    <p style="opacity:.7;margin-top:8px;text-align:center">Thank you for your payment.</p>
  </div>
</div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
const SLUG={json.dumps(slug)};
const PARAMS=new URLSearchParams(window.location.search);
const PHONE=PARAMS.get('phone')||'';
const BID=PARAMS.get('booking_id')||'';
const TOKEN=PARAMS.get('token')||PARAMS.get('t')||'';
let payInfo=null;

async function loadPaymentInfo(){{
  try{{
    const r=await fetch('/api/guest/payment-info?slug='+encodeURIComponent(SLUG)+
      '&phone='+encodeURIComponent(PHONE)+'&booking_id='+encodeURIComponent(BID)+
      '&token='+encodeURIComponent(TOKEN));
    const d=await r.json();
    if(!d.success){{
      document.getElementById('loading').textContent=d.error||'Unable to load payment info.';
      return;
    }}
    payInfo=d;
    renderPayment(d);
  }}catch(e){{
    document.getElementById('loading').textContent='Network error. Please try again.';
  }}
}}

function renderPayment(d){{
  document.getElementById('loading').style.display='none';
  document.getElementById('paymentContent').style.display='block';
  let h='';
  (d.charges||[]).forEach(c=>{{
    h+=`<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:13px">
      <span>${{c.description||c.service_type}}</span><span>₹${{parseFloat(c.amount).toFixed(0)}}</span></div>`;
  }});
  document.getElementById('chargesList').innerHTML=h;
  document.getElementById('balanceDue').textContent='Balance Due: ₹'+parseFloat(d.balance_due).toFixed(0);
  if(d.balance_due<=0){{
    document.getElementById('paidSection').style.display='block';
    return;
  }}
  const methods=d.payment_methods||[];
  if(methods.includes('razorpay')&&d.razorpay_key_id){{
    document.getElementById('razorpaySection').style.display='block';
  }}
  if(methods.includes('upi_qr')&&d.upi_qr_url){{
    document.getElementById('upiSection').style.display='block';
    document.getElementById('upiQrImg').src=d.upi_qr_url;
  }}
}}

async function initiateRazorpay(){{
  const btn=document.getElementById('rzpBtn');
  btn.disabled=true;btn.textContent='Processing...';
  try{{
    const r=await fetch('/api/guest/create-razorpay-order',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{slug:SLUG,phone:PHONE,booking_id:BID,token:TOKEN}})}});
    const d=await r.json();
    if(!d.success){{
      showToast(d.error||'Could not create order',false);
      btn.disabled=false;btn.textContent='Pay Online';
      return;
    }}
    const options={{
      key:d.key_id,
      amount:d.amount,
      currency:d.currency||'INR',
      name:d.hotel_name||'Hotel',
      description:d.description||'Hotel Payment',
      order_id:d.order_id,
      handler:async function(response){{
        await verifyPayment(response);
      }},
      modal:{{ondismiss:function(){{btn.disabled=false;btn.textContent='Pay Online';}}}}
    }};
    const rzp=new Razorpay(options);
    rzp.open();
  }}catch(e){{
    showToast('Network error',false);
    btn.disabled=false;btn.textContent='Pay Online';
  }}
}}

async function verifyPayment(response){{
  try{{
    const r=await fetch('/api/guest/verify-razorpay-payment',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{slug:SLUG,phone:PHONE,booking_id:BID,token:TOKEN,
        razorpay_order_id:response.razorpay_order_id,
        razorpay_payment_id:response.razorpay_payment_id,
        razorpay_signature:response.razorpay_signature}})}});
    const d=await r.json();
    if(d.success){{
      document.getElementById('razorpaySection').style.display='none';
      document.getElementById('upiSection').style.display='none';
      document.getElementById('paidSection').style.display='block';
      document.getElementById('balanceDue').textContent='Balance Due: ₹0';
      document.getElementById('balanceDue').style.color='#3fb950';
      showToast('Payment confirmed!');
    }}else{{
      showToast(d.error||'Verification failed',false);
      document.getElementById('rzpBtn').disabled=false;
      document.getElementById('rzpBtn').textContent='Pay Online';
    }}
  }}catch(e){{
    showToast('Network error during verification',false);
    document.getElementById('rzpBtn').disabled=false;
    document.getElementById('rzpBtn').textContent='Pay Online';
  }}
}}

loadPaymentInfo();
</script>"""
    return HTMLResponse(themed(hotel, "Online Payment", body))


@router.get("/api/guest/payment-info")
async def api_payment_info(
    slug: str = "", phone: str = "", booking_id: str = "", token: str = ""
):
    """Return balance due and available payment methods for an authenticated guest session."""
    if not slug or not phone:
        return JSONResponse({"success": False, "error": "Missing parameters"}, 400)
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, 404)
    if not hotel.get("is_active"):
        return JSONResponse({"success": False, "error": "Hotel unavailable"}, 503)

    state, _ = await _get_guest_token_session(phone.strip(), token)
    if state in ("bad", "none"):
        return JSONResponse({"success": False, "error": "Invalid session"}, 403)

    hid = hotel["id"]
    bk = await db.get_active_booking_by_phone(phone.strip(), hid)
    if not bk:
        return JSONResponse({"success": False, "error": "No active booking found"})
    if booking_id and booking_id != bk["booking_id"]:
        return JSONResponse({"success": False, "error": "Booking mismatch"}, 403)

    charges = await db.fetch(
        "SELECT id, service_type, description, amount, total, payment_status "
        "FROM stay_charges WHERE booking_id=$1 AND hotel_id=$2 AND payment_status='Pending'",
        bk["booking_id"], hid,
    )
    balance = sum(float(c.get("total") or c.get("amount") or 0) for c in charges)

    # Determine payment methods
    pay_mode = (hotel.get("payment_mode") or "razorpay").lower()
    if pay_mode == "upi_qr":
        methods = ["upi_qr"]
    elif pay_mode == "both":
        methods = ["razorpay", "upi_qr"]
    else:
        methods = ["razorpay"]

    # Build UPI QR URL if applicable
    upi_qr_url = ""
    if "upi_qr" in methods and hotel.get("upi_id"):
        from services.payment import generate_upi_qr_url
        upi_name = hotel.get("upi_display_name") or hotel.get("hotel_name", "Hotel")
        upi_qr_url = generate_upi_qr_url(
            hotel["upi_id"], upi_name, balance, bk["booking_id"]
        )

    return JSONResponse({
        "success": True,
        "payment_methods": methods,
        "balance_due": balance,
        "charges": [
            {"description": c.get("description", ""), "amount": float(c.get("total") or c.get("amount") or 0),
             "service_type": c.get("service_type", "")}
            for c in charges
        ],
        "hotel_name": hotel.get("hotel_name", ""),
        "booking_id": bk["booking_id"],
        "room_number": bk.get("room_number", ""),
        "upi_qr_url": upi_qr_url,
        "razorpay_key_id": hotel.get("razorpay_key_id", "") if "razorpay" in methods else "",
    })


@router.post("/api/guest/create-razorpay-order")
async def api_create_razorpay_order(request: Request):
    """Create a Razorpay order for the guest's pending balance."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, 400)

    slug = body.get("slug", "")
    phone = (body.get("phone") or "").strip()
    booking_id = (body.get("booking_id") or "").strip()
    token = (body.get("token") or "").strip()

    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, 404)

    state, _ = await _get_guest_token_session(phone, token)
    if state in ("bad", "none"):
        return JSONResponse({"success": False, "error": "Invalid session"}, 403)

    hid = hotel["id"]
    bk = await db.get_active_booking_by_phone(phone, hid)
    if not bk:
        return JSONResponse({"success": False, "error": "No active booking"})
    if booking_id and booking_id != bk["booking_id"]:
        return JSONResponse({"success": False, "error": "Booking mismatch"}, 403)

    balance = await db.get_balance_due(bk["booking_id"], hotel_id=hid)
    if balance <= 0:
        return JSONResponse({"success": False, "error": "No pending balance"})

    creds = await db.get_razorpay_creds(hid) or {}
    key_id = (creds.get("razorpay_key_id") or "").strip()
    key_secret = (creds.get("razorpay_secret") or "").strip()
    if not key_id or not key_secret:
        return JSONResponse({"success": False, "error": "Online payment not configured"})

    from services.payment import create_razorpay_order
    amount_paise = int(balance * 100)
    order = await create_razorpay_order(
        key_id, key_secret, amount_paise,
        receipt=bk["booking_id"],
        notes={"booking_id": bk["booking_id"], "phone": phone, "room": bk.get("room_number", "")},
    )
    if not order:
        return JSONResponse({"success": False, "error": "Could not create payment order"})

    return JSONResponse({
        "success": True,
        "order_id": order.get("id", ""),
        "amount": amount_paise,
        "key_id": key_id,
        "currency": "INR",
        "hotel_name": hotel.get("hotel_name", ""),
        "description": f"Hotel Stay - Room {bk.get('room_number', '')}",
    })


@router.post("/api/guest/verify-razorpay-payment")
async def api_verify_razorpay_payment(request: Request):
    """Verify Razorpay payment signature and record the payment."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, 400)

    slug = body.get("slug", "")
    phone = (body.get("phone") or "").strip()
    booking_id = (body.get("booking_id") or "").strip()
    token = (body.get("token") or "").strip()
    rz_order_id = (body.get("razorpay_order_id") or "").strip()
    rz_payment_id = (body.get("razorpay_payment_id") or "").strip()
    rz_signature = (body.get("razorpay_signature") or "").strip()

    if not rz_order_id or not rz_payment_id or not rz_signature:
        return JSONResponse({"success": False, "error": "Missing payment details"}, 400)

    hotel = await db.get_hotel_by_slug(slug)
    if not hotel:
        return JSONResponse({"success": False, "error": "Hotel not found"}, 404)

    state, _ = await _get_guest_token_session(phone, token)
    if state in ("bad", "none"):
        return JSONResponse({"success": False, "error": "Invalid session"}, 403)

    hid = hotel["id"]
    bk = await db.get_active_booking_by_phone(phone, hid)
    if not bk:
        return JSONResponse({"success": False, "error": "No active booking"})
    if booking_id and booking_id != bk["booking_id"]:
        return JSONResponse({"success": False, "error": "Booking mismatch"}, 403)

    creds = await db.get_razorpay_creds(hid) or {}
    key_secret = (creds.get("razorpay_secret") or "").strip()
    if not key_secret:
        return JSONResponse({"success": False, "error": "Payment not configured"})

    from services.payment import verify_razorpay_payment_signature
    if not verify_razorpay_payment_signature(rz_order_id, rz_payment_id, rz_signature, key_secret):
        return JSONResponse({"success": False, "error": "Payment verification failed"}, 400)

    # Record the payment
    balance = await db.get_balance_due(bk["booking_id"], hotel_id=hid)
    bid = bk["booking_id"]
    room = bk.get("room_number", "")
    name = bk.get("guest_name", "")

    await db.insert_payment_log({
        "booking_id": bid,
        "guest_phone": phone,
        "room_number": room,
        "guest_name": name,
        "amount": balance,
        "payment_method": "Online",
        "reference": rz_payment_id,
        "hotel_id": hid,
    })
    await db.mark_charges_paid(bid, "Online", rz_payment_id, hotel_id=hid)
    await db.execute(
        "UPDATE bookings SET total_paid=total_paid+$1,updated_at=NOW() WHERE booking_id=$2 AND hotel_id=$3",
        balance, bid, hid,
    )

    # Notify guest via WhatsApp
    try:
        await send_text(hotel["instance_name"], phone,
            f"✅ *Payment Confirmed!*\n💰 ₹{balance:.0f} received online.\n"
            f"🏨 Room: {room}\nThank you! 🙏")
    except Exception:
        pass

    return JSONResponse({"success": True, "message": "Payment confirmed"})
