"""
services/security.py
Crypto helpers — currently:
  - verify_razorpay_signature: HMAC-SHA256 webhook signature check
"""
import hmac
import hashlib


def verify_razorpay_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Verify the X-Razorpay-Signature header against the raw request body.

    Razorpay computes:  HMAC_SHA256(webhook_secret, raw_body).hexdigest()
    and sends it in the X-Razorpay-Signature header.

    Returns True iff the signature matches. Constant-time comparison.
    """
    if not signature or not secret or not raw_body:
        return False
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
