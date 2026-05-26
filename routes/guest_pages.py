from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from services import database as db
from services.cache import get_session, set_session, set_room, calc_ttl, get_room as cache_get_room
from services.whatsapp import send_text, send_to_phones
from services.helpers import booking_id as gen_bk, calc_nights, fmt_date, ist_now, categorize_service, request_id as gen_sr
from datetime import date
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def themed(hotel: dict, title: str, body: str) -> str:
    pri = hotel.get("primary_color","#c8a84b")
    sec = hotel.get("secondary_color","#1a2942")
    bg  = hotel.get("background_color","#0d1117")
    btn = hotel.get("button_color","#c8a84b")
    txt = hotel.get("text_color","#ffffff")
    fnt = hotel.get("font_choice","Outfit")
    logo= hotel.get("logo_url","")
    hn  = hotel.get("hotel_name","Hotel")
    tag = hotel.get("tagline","")
    addr= hotel.get("address","")
    city= hotel.get("city","")
    em  = hotel.get("emergency_number","")
    maps= hotel.get("google_maps_url","")
    email=hotel.get("hotel_email","")
    ci_t= hotel.get("check_in_time","2:00 PM")
    co_t= hotel.get("checkout_time_display","11:00 AM")
    logo_h = f'<img src="{logo}" alt="{hn}" style="height:60px;object-fit:contain;display:block;margin:0 auto 10px">' if logo else ""
    maps_h = f'<a href="{maps}" target="_blank" style="color:{pri};font-size:12px">📍 Get Directions</a>' if maps else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>{title} — {hn}</title>
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
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404,"Hotel not found")
    hid = hotel["id"]
    rooms = await db.get_all_rooms(hid)
    room_opts = ""
    for r in rooms:
        occ = await cache_get_room(r["room_number"])
        if not occ:
            room_opts += f'<option value="{r["room_number"]}" data-secret="{r["qr_secret"]}" data-rate="{r["room_rate"] or 0}">{r["room_number"]} — {r["room_type"]} (₹{r["room_rate"] or 0}/night)</option>'

    body = f"""
<div class="card"><div class="ct">🏨 Guest Registration</div>
  <p style="font-size:13px;opacity:.65">{hotel.get("welcome_message","Welcome! Please fill your details.")}</p>
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
const CLOUD="{hotel.get('cloudinary_cloud_name','')}",PRESET="{hotel.get('cloudinary_upload_preset','')}",SLUG="{slug}";

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
      const r=await fetch('/api/guest/lookup?slug={slug}&phone='+encodeURIComponent(p));
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
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    hid = hotel["id"]
    services = await db.get_services(hid)
    pri = hotel.get("primary_color","#c8a84b")

    cats: dict = {}
    for s in services:
        cats.setdefault(s.get("category","Other"),[]).append(s)

    svc_html = ""
    for cat, items in cats.items():
        svc_html += f'<div class="ctitle">🔹 {cat}</div>'
        for s in items:
            p = float(s.get("price",0))
            ps = f"₹{p:.0f}" if p>0 else "Free"
            desc = s.get("description","") or ""
            sname = s['service_name']
            sname_js = sname.replace("\\", "\\\\").replace("'", "\\'")
            desc_html = f'<div style="font-size:11px;opacity:.55;margin-top:2px">{desc}</div>' if desc else ''
            svc_html += f"""<div class="scard" onclick="reqSvc('{sname_js}',{p})">
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
    <div style="font-size:12px;opacity:.55;margin-bottom:12px">⏰ Hours: {hotel.get('svc_open_hour',7)}AM – {hotel.get('svc_close_hour',23)}PM · Checkout: {hotel.get('checkout_time_display','11:00 AM')}</div>
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
    body:JSON.stringify({{slug:'{slug}',room:gRoom,phone:gPhone,booking_id:gBid,service:svc}})}});
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
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: raise HTTPException(404)
    pri = hotel.get("primary_color","#c8a84b")
    body = f"""
<div class="card"><div class="ct">💰 View Your Bill</div>
  <label>Your WhatsApp Number</label>
  <input type="tel" id="bPhone" placeholder="91XXXXXXXXXX">
  <button class="btn" onclick="loadBill()">View My Bill →</button>
</div>
<div id="bDiv" style="display:none"></div>
<script>
async function loadBill(){{
  const phone=document.getElementById('bPhone').value.trim();
  if(!phone){{showToast('Enter your phone number',false);return;}}
  const r=await fetch('/api/guest/bill?phone='+phone+'&slug={slug}');
  const d=await r.json();
  if(!d.found){{showToast('No active booking found',false);return;}}
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
    if room_row.get("qr_secret") and room_row["qr_secret"] != secret:
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
    sess = {"phone":phone,"name":name,"room":room,"bookingId":bk_id,
            "checkinDate":ci,"checkoutDate":co,"status":"AWAITING_APPROVAL",
            "orders":[],"sessionType":"HOTEL","hotelId":hid,
            "hotelName":hotel["hotel_name"],"createdAt":ist_now().isoformat(),"TTL":ttl}
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
async def api_lookup(slug: str = "", phone: str = ""):
    """
    Returning-guest auto-fill. If this phone has stayed at the hotel before,
    return name + ID-type so the registration form can pre-fill them.
    Always returns {found,...} — never errors out.
    Sensitive fields (ID number, photos) are NOT returned — guest re-enters every visit.
    """
    try:
        hotel = await db.get_hotel_by_slug(slug)
        if not hotel:
            return JSONResponse({"found": False})
        info = await db.lookup_returning_guest(hotel["id"], (phone or "").strip())
        return JSONResponse(info or {"found": False})
    except Exception:
        return JSONResponse({"found": False})

@router.get("/api/guest/charges")
async def api_charges(booking_id: str = ""):
    return JSONResponse({"charges": await db.get_charges_for_booking(booking_id)})

@router.get("/api/guest/bill")
async def api_bill(phone: str = "", slug: str = ""):
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel: return JSONResponse({"found":False})
    bk = await db.get_active_booking_by_phone(phone, hotel["id"])
    if not bk: return JSONResponse({"found":False})
    charges = await db.get_charges_for_booking(bk["booking_id"])
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
    pri = hotel.get("primary_color", "#c8a84b")
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
const SLUG='{slug}';
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
  const r=await fetch('/api/guest/food/my-orders?slug='+SLUG+'&phone='+gPhone);
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
async def api_food_my_orders(slug: str = "", phone: str = ""):
    hotel = await db.get_hotel_by_slug(slug)
    if not hotel or not phone:
        return JSONResponse({"orders": []})
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
