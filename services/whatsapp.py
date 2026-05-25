import httpx, base64, logging, urllib.parse
from config.settings import EVOLUTION_API_URL, EVOLUTION_API_KEY

logger = logging.getLogger(__name__)
H = {"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY}

async def send_text(instance: str, phone: str, message: str) -> bool:
    if not phone or not message: return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{EVOLUTION_API_URL}/message/sendText/{instance}",
                             json={"number": phone, "text": message}, headers=H)
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"WA text error: {e}"); return False

async def send_to_phones(instance: str, phones: list, message: str):
    for p in phones:
        if p: await send_text(instance, p, message)

async def send_media_b64(instance: str, phone: str, b64: str,
                          caption: str = "", mtype: str = "document", fname: str = "bill.pdf") -> bool:
    mime = "application/pdf" if mtype == "document" else "image/png"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{EVOLUTION_API_URL}/message/sendMedia/{instance}",
                             json={"number": phone, "mediatype": mtype, "mimetype": mime,
                                   "caption": caption, "media": b64, "fileName": fname}, headers=H)
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"WA media error: {e}"); return False

async def send_image_b64(instance: str, phone: str, b64: str, caption: str = "") -> bool:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{EVOLUTION_API_URL}/message/sendMedia/{instance}",
                             json={"number": phone, "mediatype": "image",
                                   "mimetype": "image/png", "caption": caption, "media": b64}, headers=H)
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"WA image error: {e}"); return False

async def fetch_upi_qr(upi_id: str, upi_name: str, amount: float, room: str, bid: str) -> str:
    upi_link = (f"upi://pay?pa={upi_id}&pn={urllib.parse.quote(upi_name)}"
                f"&am={int(amount)}&tn=Room-{room}-{bid}&cu=INR")
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=512x512&margin=10&data={urllib.parse.quote(upi_link)}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(qr_url)
            if r.status_code == 200: return base64.b64encode(r.content).decode()
    except Exception as e:
        logger.error(f"QR error: {e}")
    return ""

async def create_razorpay_link(key_id: str, secret: str, amount: float, desc: str, phone: str):
    try:
        async with httpx.AsyncClient(timeout=15, auth=(key_id, secret)) as c:
            r = await c.post("https://api.razorpay.com/v1/payment_links",
                             json={"amount": int(amount*100), "currency": "INR",
                                   "description": desc, "customer": {"contact": phone},
                                   "notify": {"sms": False, "email": False}})
            if r.status_code in (200,201):
                d = r.json()
                return d.get("short_url") or d.get("payment_link_url")
    except Exception as e:
        logger.error(f"Razorpay error: {e}")
    return None
