"""
services/payment.py - Payment utility functions for online payments.
Razorpay order creation via httpx, UPI QR URL generation, signature verification.
"""
import hmac
import hashlib
import urllib.parse
from typing import Optional, Dict

import httpx


def generate_upi_qr_url(upi_id: str, upi_name: str, amount: float, reference: str) -> str:
    """Build a QR code URL via qrserver.com that encodes a UPI deep link."""
    upi_link = (
        f"upi://pay?pa={upi_id}"
        f"&pn={urllib.parse.quote(upi_name)}"
        f"&am={amount}"
        f"&tn={urllib.parse.quote(reference)}"
        f"&cu=INR"
    )
    return (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size=400x400&margin=10&data={urllib.parse.quote(upi_link)}"
    )


async def create_razorpay_order(
    key_id: str,
    key_secret: str,
    amount_paise: int,
    receipt: str,
    notes: Optional[Dict] = None,
) -> Optional[Dict]:
    """Create a Razorpay order via their API. Returns the order dict or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.razorpay.com/v1/orders",
                auth=(key_id, key_secret),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": receipt,
                    "notes": notes or {},
                },
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


def verify_razorpay_payment_signature(
    order_id: str, payment_id: str, signature: str, key_secret: str
) -> bool:
    """Verify the Razorpay checkout signature using HMAC-SHA256."""
    message = f"{order_id}|{payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
