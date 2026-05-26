"""
services/formc.py — Form C (Foreigners Reporting) helpers.

Indian law (Foreigners Act 1946; Registration of Foreigners Rules 1992 as
re-issued under the Immigration & Foreigners Act 2025) requires every
"keeper of a hotel" — including guest houses, hostels, homestays and
medical institutions — to electronically transmit Form C to the local
Foreigners Regional Registration Office (FRRO) within 24 hours of a foreign
national checking in.

Filing is done through the FRRO portal at https://indianfrro.gov.in (or the
"Indian Visa Su-Swagatam" mobile app). Failure to file is a punishable
offence — up to ₹500,000 in fines and 5 years' imprisonment under the 2025
Act.

Two filing modes are supported by the portal:
    1. Single guest entry via the web form
    2. Bulk upload via XLSX/CSV (preferred for properties with multiple
       foreign guests)

This module produces the bulk-upload CSV in the column order the portal
expects, plus a JSON payload suitable for any future direct API integration.
We deliberately do NOT call the FRRO API directly: at the time of writing,
the portal does not expose a public API. Operators export the CSV from this
service, sign in to the FRRO portal, and bulk-upload it. After upload the
operator marks the booking as filed inside HotelFlow with the FRRO reference
number.

This module is pure / DB-free; the route layer is responsible for fetching
booking + hotel rows.
"""
from __future__ import annotations
import csv
import io
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape


# Bulk-upload column headers, in the exact order the FRRO portal CSV template
# uses. Operators copy this CSV directly into the upload field. The names
# match the field labels on the indianfrro.gov.in C-Form template; if the
# template changes, update this list — every other call site uses these
# names by reference.
FORMC_CSV_HEADERS = [
    "S.No",
    "Name",
    "Nationality",
    "Passport No",
    "Date of Birth",
    "Sex",
    "Place of Issue",
    "Date of Issue",
    "Date of Expiry",
    "Visa No",
    "Visa Type",
    "Visa Issue Place",
    "Visa Issue Date",
    "Visa Expiry Date",
    "Date of Arrival in India",
    "Port of Arrival in India",
    "Last Country Visited",
    "Next Destination",
    "Purpose of Visit",
    "Address in India",
    "Date of Arrival at Hotel",
    "Time of Arrival at Hotel",
    "Date of Departure from Hotel",
    "Time of Departure from Hotel",
    "Hotel Name",
    "Hotel Address",
    "Remarks",
]

# Common visa types the FRRO portal accepts. Used to validate / normalise
# what the operator enters at check-in.
KNOWN_VISA_TYPES = [
    "Tourist", "Business", "Employment", "Student", "Medical",
    "Conference", "Journalist", "Diplomatic", "Transit", "OCI", "PIO",
    "Entry (X)", "Research", "Project", "Missionary", "Other",
]


def _fmt_date(d) -> str:
    """Format any date-ish value as DD/MM/YYYY (FRRO portal format)."""
    if not d:
        return ""
    if isinstance(d, str):
        s = d.split("T")[0]
        # Already DD/MM/YYYY
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            return s
        # ISO YYYY-MM-DD
        try:
            d = datetime.fromisoformat(s)
        except Exception:
            return s
    try:
        return d.strftime("%d/%m/%Y")
    except Exception:
        return str(d)


def _fmt_time(d) -> str:
    if not d:
        return "12:00"
    if isinstance(d, str):
        if re.match(r"^\d{2}:\d{2}", d):
            return d[:5]
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00"))
        except Exception:
            return "12:00"
    try:
        return d.strftime("%H:%M")
    except Exception:
        return "12:00"


def is_filing_required(booking: Dict) -> bool:
    """A FormC filing is required iff guest is foreign AND not yet filed."""
    if not booking:
        return False
    if not booking.get("is_foreign_guest"):
        return False
    return (booking.get("formc_status") or "Pending") != "Filed"


def filing_deadline(booking: Dict) -> Optional[datetime]:
    """24 hours after arrival at hotel (= check-in). None if no check-in date."""
    ci = booking.get("checkin_date")
    if not ci:
        return None
    if isinstance(ci, str):
        try:
            ci = datetime.fromisoformat(ci.replace("Z", "+00:00"))
        except Exception:
            return None
    return ci + timedelta(hours=24)


def is_overdue(booking: Dict, now: Optional[datetime] = None) -> bool:
    """True if the 24-hour FormC window has expired and the booking hasn't been filed."""
    if not is_filing_required(booking):
        return False
    deadline = filing_deadline(booking)
    if not deadline:
        return False
    return (now or datetime.utcnow()) > deadline.replace(tzinfo=None)


def booking_to_formc_row(booking: Dict, hotel: Dict, sno: int = 1) -> Dict[str, str]:
    """
    Normalise a (booking, hotel) pair into the FormC CSV row shape. Every
    value is a string and the keys exactly match FORMC_CSV_HEADERS.
    """
    arrival_dt = booking.get("checkin_date") or datetime.utcnow()
    depart_dt  = booking.get("checkout_date") or ""
    full_addr  = ", ".join([s for s in [hotel.get("address", ""), hotel.get("city", "")] if s])

    return {
        "S.No":                          str(sno),
        "Name":                          (booking.get("guest_name") or "").strip(),
        "Nationality":                   (booking.get("nationality") or "").strip(),
        "Passport No":                   (booking.get("passport_no") or "").strip(),
        "Date of Birth":                 _fmt_date(booking.get("date_of_birth") or ""),
        "Sex":                           (booking.get("sex") or "").strip(),
        "Place of Issue":                (booking.get("passport_place_of_issue") or "").strip(),
        "Date of Issue":                 _fmt_date(booking.get("passport_issue_date") or ""),
        "Date of Expiry":                _fmt_date(booking.get("passport_expiry_date") or ""),
        "Visa No":                       (booking.get("visa_no") or "").strip(),
        "Visa Type":                     (booking.get("visa_type") or "").strip(),
        "Visa Issue Place":              (booking.get("visa_issue_place") or "").strip(),
        "Visa Issue Date":               _fmt_date(booking.get("visa_issue_date") or ""),
        "Visa Expiry Date":              _fmt_date(booking.get("visa_expiry_date") or ""),
        "Date of Arrival in India":      _fmt_date(booking.get("arrival_in_india_date") or ""),
        "Port of Arrival in India":      (booking.get("arrival_in_india_port") or "").strip(),
        "Last Country Visited":          (booking.get("last_country_visited") or "").strip(),
        "Next Destination":              (booking.get("next_destination") or "").strip(),
        "Purpose of Visit":              (booking.get("purpose_of_visit") or "").strip(),
        "Address in India":              full_addr,
        "Date of Arrival at Hotel":      _fmt_date(arrival_dt),
        "Time of Arrival at Hotel":      _fmt_time(arrival_dt),
        "Date of Departure from Hotel":  _fmt_date(depart_dt),
        "Time of Departure from Hotel":  _fmt_time(depart_dt or "12:00"),
        "Hotel Name":                    hotel.get("hotel_name", ""),
        "Hotel Address":                 full_addr,
        "Remarks":                       (booking.get("formc_remarks") or "").strip(),
    }


def build_csv(bookings: List[Dict], hotel: Dict) -> str:
    """Return a CSV string for FRRO portal bulk upload."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FORMC_CSV_HEADERS, lineterminator="\n")
    w.writeheader()
    for i, b in enumerate(bookings, 1):
        w.writerow(booking_to_formc_row(b, hotel, i))
    return out.getvalue()


def build_xml(booking: Dict, hotel: Dict) -> str:
    """
    Build a single-guest FormC XML payload.

    There is no published FRRO XSD; this XML therefore mirrors the field
    layout of the FRRO portal's C-Form web form, in the same case and order.
    Some operators / state police forces accept this XML as a printout
    attachment when a foreign guest cannot be filed online due to portal
    downtime. The schema is intentionally simple so a clerk can read it.
    """
    row = booking_to_formc_row(booking, hotel)
    body = "\n  ".join(
        f"<{_tag(k)}>{xml_escape(v)}</{_tag(k)}>" for k, v in row.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<FormC version="2025.1">
  <FilingMeta>
    <BookingId>{xml_escape(str(booking.get("booking_id","")))}</BookingId>
    <HotelGSTIN>{xml_escape(hotel.get("gstin",""))}</HotelGSTIN>
    <HotelState>{xml_escape(hotel.get("state_code",""))}</HotelState>
    <GeneratedAt>{datetime.utcnow().isoformat()}Z</GeneratedAt>
  </FilingMeta>
  {body}
</FormC>
"""


def _tag(label: str) -> str:
    """CSV header → XML tag name (PascalCase, alnum)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", label).strip()
    return "".join(p.capitalize() for p in cleaned.split())


def validate_foreign_guest(payload: Dict) -> List[str]:
    """
    Return a list of human-readable error strings for missing fields. An
    empty list means the guest is FormC-fileable.

    The portal mandates: name, nationality, passport, visa no/type, arrival
    date in India, and address. Everything else is optional but recommended.
    """
    errors: List[str] = []
    required = [
        ("guest_name",            "Guest name"),
        ("nationality",           "Nationality"),
        ("passport_no",           "Passport number"),
        ("visa_no",               "Visa number"),
        ("visa_type",             "Visa type"),
        ("arrival_in_india_date", "Date of arrival in India"),
    ]
    for key, label in required:
        if not (payload.get(key) or "").strip() if isinstance(payload.get(key), str) else not payload.get(key):
            errors.append(f"{label} is required")

    pp = (payload.get("passport_no") or "").strip().upper()
    if pp and not re.match(r"^[A-Z0-9]{5,20}$", pp):
        errors.append("Passport number looks invalid (5–20 alphanumeric characters)")

    vt = (payload.get("visa_type") or "").strip()
    if vt and vt not in KNOWN_VISA_TYPES:
        # Don't reject — the FRRO portal accepts free text, but warn.
        errors.append(f"Visa type '{vt}' is not in the standard list (will be sent as-is)")

    return errors
