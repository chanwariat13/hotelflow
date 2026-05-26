"""
services/gst.py — Indian GST split helper.

Indian GST (CGST Act 2017 / IGST Act 2017) requires a tax invoice to split
tax into CGST + SGST when the supplier and place-of-supply are in the SAME
state, and into a single IGST line when they differ.

For hotel accommodation (HSN/SAC 996311) and restaurant service (996331)
the place-of-supply rule is "location of the immovable property" / "where the
service is rendered" — i.e. the hotel's own state — so for a typical stay the
split is governed by:

    intra-state  (hotel_state == guest_state)  -> CGST + SGST (each = rate/2)
    inter-state  (hotel_state != guest_state)  -> IGST  (= rate)

A guest staying in a hotel in their own state is intra-state regardless of
where they "live"; what matters legally is the place of supply, which for a
hotel is the hotel's state. We expose `compute_split` so callers can override
`is_inter_state` directly when they're computing tax on a non-room item that
follows different POS rules (e.g. event/banquet billed to a corporate office
in another state).

This module has no DB dependency — pure functions.

References:
- Section 12(3) IGST Act: place of supply for accommodation = location of property
- HSN 996311 = hotel accommodation, 996331 = restaurant service, 996312 = camp/site
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple


# Default HSN/SAC codes used by hotels in India.
HSN_ROOM       = "996311"
HSN_RESTAURANT = "996331"
HSN_LAUNDRY    = "999719"
# 996421 = "Passenger transport services" (the canonical SAC for transferring
# guests in/out by car/cab). The previous value "996601" did not exist in the
# CGST SAC schedule and would be rejected by GST returns / Tally.
HSN_TRANSPORT  = "996421"
HSN_OTHER      = "999799"

SERVICE_TO_HSN = {
    "Room":         HSN_ROOM,
    "Food":         HSN_RESTAURANT,
    "Laundry":      HSN_LAUNDRY,
    "Transport":    HSN_TRANSPORT,
    "Housekeeping": HSN_OTHER,
    "Other":        HSN_OTHER,
}


def hsn_for_service(service_type: str) -> str:
    """Return the standard HSN/SAC code for one of our service categories."""
    return SERVICE_TO_HSN.get(service_type or "Other", HSN_OTHER)


def _q(x) -> Decimal:
    """Quantise to 2 decimal places, half-up — same rounding as a Tally bill."""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_inter_state(seller_state_code: Optional[str],
                   place_of_supply_code: Optional[str]) -> bool:
    """
    True if the supply is inter-state (use IGST), False otherwise (use CGST+SGST).
    A missing/blank seller state defaults to intra-state — safest option for a
    bill that pre-dates the seller having configured their GSTIN.
    """
    s = (seller_state_code or "").strip()
    p = (place_of_supply_code or "").strip()
    if not s or not p:
        return False
    return s != p


def compute_split(taxable_amount,
                  rate_percent,
                  seller_state_code: Optional[str] = None,
                  place_of_supply_code: Optional[str] = None,
                  inter_state: Optional[bool] = None) -> dict:
    """
    Compute CGST / SGST / IGST amounts for one taxable line.

    Args:
        taxable_amount:       pre-tax amount (₹)
        rate_percent:         total GST rate as percent, e.g. 18, 12, 5
        seller_state_code:    GSTIN-style 2-digit state code of the hotel/restaurant
        place_of_supply_code: 2-digit state code where the supply is consumed
        inter_state:          force inter-state if not None (overrides the codes)

    Returns:
        dict with cgst, sgst, igst, total_tax, rate, is_inter_state.

    All money fields are returned as floats rounded to 2dp so they round-trip
    cleanly through JSON / Postgres NUMERIC(10,2).
    """
    amt  = _q(taxable_amount or 0)
    rate = Decimal(str(rate_percent or 0))

    inter = bool(inter_state) if inter_state is not None else is_inter_state(seller_state_code, place_of_supply_code)
    total_tax = _q(amt * rate / Decimal("100"))

    if inter:
        cgst = sgst = Decimal("0.00")
        igst = total_tax
    else:
        half = _q(total_tax / Decimal("2"))
        cgst = half
        sgst = _q(total_tax - half)  # absorbs the rounding remainder so cgst+sgst == total_tax
        igst = Decimal("0.00")

    return {
        "rate":           float(rate),
        "is_inter_state": inter,
        "cgst":           float(cgst),
        "sgst":           float(sgst),
        "igst":           float(igst),
        "total_tax":      float(total_tax),
        "taxable":        float(amt),
        "total":          float(_q(amt + total_tax)),
    }


def split_existing_tax(taxable_amount, tax_amount,
                       seller_state_code: Optional[str] = None,
                       place_of_supply_code: Optional[str] = None,
                       inter_state: Optional[bool] = None) -> Tuple[float, float, float]:
    """
    For legacy rows that already have a flat `tax` value but no CGST/SGST/IGST
    breakdown, derive the split without re-computing from the rate. Returns
    (cgst, sgst, igst).
    """
    inter = bool(inter_state) if inter_state is not None else is_inter_state(seller_state_code, place_of_supply_code)
    tax = _q(tax_amount or 0)
    if inter:
        return 0.0, 0.0, float(tax)
    half = _q(tax / Decimal("2"))
    return float(half), float(_q(tax - half)), 0.0


# Indian state code → name (used by FormC + invoice rendering). Two-digit codes
# are the first two digits of any GSTIN issued in that state.
INDIAN_STATE_CODES = {
    "01": "Jammu & Kashmir",   "02": "Himachal Pradesh",   "03": "Punjab",
    "04": "Chandigarh",        "05": "Uttarakhand",        "06": "Haryana",
    "07": "Delhi",             "08": "Rajasthan",          "09": "Uttar Pradesh",
    "10": "Bihar",             "11": "Sikkim",             "12": "Arunachal Pradesh",
    "13": "Nagaland",          "14": "Manipur",            "15": "Mizoram",
    "16": "Tripura",           "17": "Meghalaya",          "18": "Assam",
    "19": "West Bengal",       "20": "Jharkhand",          "21": "Odisha",
    "22": "Chhattisgarh",      "23": "Madhya Pradesh",     "24": "Gujarat",
    "25": "Daman & Diu",       "26": "Dadra & Nagar Haveli","27": "Maharashtra",
    "28": "Andhra Pradesh (Old)","29": "Karnataka",         "30": "Goa",
    "31": "Lakshadweep",       "32": "Kerala",             "33": "Tamil Nadu",
    "34": "Puducherry",        "35": "Andaman & Nicobar",  "36": "Telangana",
    "37": "Andhra Pradesh",    "38": "Ladakh",             "97": "Other Territory",
    "99": "Other Country",
}


def state_code_from_gstin(gstin: str) -> str:
    """Extract the 2-digit state code from a GSTIN. '' if invalid."""
    g = (gstin or "").strip().upper()
    if len(g) >= 2 and g[:2].isdigit() and g[:2] in INDIAN_STATE_CODES:
        return g[:2]
    return ""


def state_name(code: str) -> str:
    return INDIAN_STATE_CODES.get((code or "").strip(), "")
