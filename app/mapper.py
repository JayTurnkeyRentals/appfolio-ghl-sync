"""Maps an AppFolio Guest Card Inquiry record to GoHighLevel Contact/Note payloads.

Field names below match the live AppFolio Reports API response for the
`guest_card_inquiries` report (verified against a real sample record on
2026-08-07). Notable quirks:
  - `name` is formatted as "Last, First" (no separate first/last name fields).
  - `property` is a single string combining property name + address.
  - `guest_card_id` is returned as an integer, not a string.
  - `assigned_user` is a name string, sometimes suffixed with " (Hidden)".

IMPORTANT DATA MODEL NOTE:
  `guest_card_id` identifies the PERSON (AppFolio's guest card), not a single
  inquiry. One guest card can have many inquiries (e.g. interest in several
  units), and the report returns one ROW PER INQUIRY -- multiple rows can
  share the same guest_card_id. In GHL terms: one guest_card_id -> one
  contact, one inquiry -> one note. Do not use guest_card_id alone as an
  idempotency key for notes/sync-state -- use `inquiry_id()` below.
"""
import hashlib
import re
from datetime import date, datetime
from typing import Any, Optional

GUEST_CARD_ID_FIELD = "guest_card_id"
FULL_NAME_FIELD = "name"
EMAIL_FIELD = "email_address"
PHONE_FIELD = "phone_number"
PROPERTY_NAME_FIELD = "property"
UNIT_FIELD = "unit"
SOURCE_FIELD = "source"
STATUS_FIELD = "status"
LEAD_TYPE_FIELD = "lead_type"
COMMENTS_FIELD = "notes"
RECEIVED_ON_FIELD = "received"
ASSIGNED_USER_FIELD = "assigned_user"
CONTACT_NOTE_FIELDS = (
    ("move_in_preference", "Move In Preference"),
    ("max_rent", "Max Rent"),
    ("bed_bath_preference", "Bed Bath Preference"),
    ("pet_preference", "Pet Preference"),
    ("monthly_income", "Monthly Income"),
    ("credit_score", "Credit Score"),
)

DEFAULT_TAG = "appfolio-guest-card"


def _split_name(guest_card: dict[str, Any]) -> tuple[str, str]:
    """AppFolio returns `name` as "Last, First" -- split and reorder to first/last."""
    full_name = (guest_card.get(FULL_NAME_FIELD) or "").strip()
    if not full_name:
        return "", ""

    if "," in full_name:
        last, _, first = full_name.partition(",")
        return first.strip(), last.strip()

    parts = full_name.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def guest_card_id(guest_card: dict[str, Any]) -> str:
    """Identifies the PERSON (AppFolio guest card). Stable across all of that
    person's inquiries -- do NOT treat this as a per-inquiry idempotency key."""
    value = guest_card.get(GUEST_CARD_ID_FIELD)
    if not value:
        raise ValueError(f"Guest card record missing '{GUEST_CARD_ID_FIELD}': {guest_card}")
    return str(value)


def inquiry_id(guest_card: dict[str, Any]) -> str:
    """Identifies a single INQUIRY (one unit interest event) within a guest card.

    A guest card can have multiple inquiries over time (different units,
    different dates). We combine guest_card_id with the inquiry's received
    timestamp, property, and unit to get a stable per-inquiry key. `received`
    is a full ISO timestamp (e.g. '2026-08-12T07:17:25Z'), so this is
    effectively unique even if someone inquires about the same unit twice.
    """
    gc_id = guest_card_id(guest_card)
    received = (guest_card.get(RECEIVED_ON_FIELD) or "").strip()
    property_name = (guest_card.get(PROPERTY_NAME_FIELD) or "").strip()
    unit = (guest_card.get(UNIT_FIELD) or "").strip()

    if not received:
        raise ValueError(
            f"Guest card {gc_id}'s inquiry is missing '{RECEIVED_ON_FIELD}'; "
            "cannot build a stable per-inquiry id from this record."
        )

    return f"{gc_id}::{received}::{property_name}::{unit}"


def received_date(guest_card: dict[str, Any]) -> Optional[date]:
    """The calendar date (UTC) this inquiry was received on, used to check
    who's out of office that day. Returns None if unparseable/missing."""
    received = (guest_card.get(RECEIVED_ON_FIELD) or "").strip()
    if not received:
        return None
    try:
        return datetime.fromisoformat(received.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def full_name(guest_card: dict[str, Any]) -> str:
    """Human-readable "First Last" name, used for opportunity naming (the raw
    `name` field is "Last, First" -- see _split_name)."""
    first, last = _split_name(guest_card)
    return f"{first} {last}".strip()


def inquiry_address(guest_card: dict[str, Any]) -> Optional[str]:
    """Return the address suffix from AppFolio's combined property field."""
    property_value = (guest_card.get(PROPERTY_NAME_FIELD) or "").strip()
    if not property_value:
        return None
    _, separator, address = property_value.partition(" - ")
    return address.strip() if separator and address.strip() else property_value


def normalize_user_name(name: Optional[str]) -> Optional[str]:
    """Strip parenthetical suffixes (e.g. " (Hidden)") and normalize case/whitespace
    so AppFolio assigned-user names can be matched against GHL user names."""
    if not name:
        return None
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip().lower()
    return cleaned or None


def contact_dedup_key(guest_card: dict[str, Any]) -> Optional[str]:
    """Secondary key for detecting the same person across two DIFFERENT
    guest_card_ids (e.g. AppFolio accidentally created a duplicate guest
    card). This is a fallback only -- the primary contact identity is now
    guest_card_id itself (see mapper.guest_card_id / sync.py contact
    resolution), since one guest_card_id already reliably means one person."""
    email = (guest_card.get(EMAIL_FIELD) or "").strip().lower()
    if email:
        return f"email:{email}"

    phone_digits = re.sub(r"\D", "", guest_card.get(PHONE_FIELD) or "")
    if phone_digits:
        return f"phone:{phone_digits}"

    return None


def guest_card_to_contact_payload(guest_card: dict[str, Any]) -> dict[str, Any]:
    first_name, last_name = _split_name(guest_card)

    payload: dict[str, Any] = {
        "firstName": first_name or None,
        "lastName": last_name or None,
        "email": guest_card.get(EMAIL_FIELD) or None,
        "phone": guest_card.get(PHONE_FIELD) or None,
        "address1": inquiry_address(guest_card),
        "source": guest_card.get(SOURCE_FIELD) or "AppFolio Guest Card",
        "tags": [DEFAULT_TAG],
    }
    # Drop keys with None values -- GHL treats absent and null differently for some fields.
    return {key: value for key, value in payload.items() if value is not None}


def note_dedup_marker(inquiry_key: str) -> str:
    """Exact-match marker embedded in a note body so re-runs can detect whether
    a note for this SPECIFIC INQUIRY was already created (e.g. a previous run
    created the note but crashed before recording state), without relying on
    fuzzy substring matching against unit/date, which produces false positives.

    Keyed by inquiry_key (not guest_card_id) -- a marker keyed only by
    guest_card_id would match ANY note for that person and incorrectly treat
    every subsequent inquiry as a duplicate of the first one.
    """
    digest = hashlib.sha1(inquiry_key.encode("utf-8")).hexdigest()[:12]
    return f"[inq:{digest}]"


def guest_card_to_note(guest_card: dict[str, Any], inquiry_key: str) -> tuple[str, Optional[str]]:
    """Returns a note body with inquiry and screening preferences, plus title.

    The body always ends with a hidden dedup marker for this specific inquiry
    (see note_dedup_marker) so re-run logic can check for an exact match
    instead of guessing from content.
    """
    lines = []
    property_name = guest_card.get(PROPERTY_NAME_FIELD)
    if property_name:
        lines.append(f"Property: {property_name}")

    unit = guest_card.get(UNIT_FIELD)
    if unit:
        lines.append(f"Unit: {unit}")

    status = guest_card.get(STATUS_FIELD)
    if status:
        lines.append(f"Status: {status}")

    lead_type = guest_card.get(LEAD_TYPE_FIELD)
    if lead_type:
        lines.append(f"Lead type: {lead_type}")

    for field_name, label in CONTACT_NOTE_FIELDS:
        value = guest_card.get(field_name)
        if value is None:
            value = guest_card.get(label)
        if value is not None and str(value).strip():
            lines.append(f"{label}: {value}")

    received_on = guest_card.get(RECEIVED_ON_FIELD)
    if received_on:
        lines.append(f"Received on: {received_on}")

    comments = guest_card.get(COMMENTS_FIELD)
    if comments:
        lines.append("")
        lines.append("Comments:")
        lines.append(str(comments))

    body = "\n".join(lines) if lines else "AppFolio guest card inquiry (no additional details)."
    body = f"{body}\n\n{note_dedup_marker(inquiry_key)}"
    return body, "AppFolio Guest Card"
