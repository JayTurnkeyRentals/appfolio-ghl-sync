"""Maps each AppFolio unit vacancy row to one GHL Property record.

AppFolio `unit_id` is the unique sync key and GHL field. Unit name and address
are used instead of parent-property data.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

UNIT_ID_FIELD = "unit_id"
PROPERTY_NAME_FIELD = "property_name"
ADDRESS_FIELD = "address"
UNIT_FIELD = "unit"
UNIT_STATUS_FIELD = "unit_status"
DAYS_VACANT_FIELD = "days_vacant"
ADVERTISED_RENT_FIELD = "advertised_rent"
BED_AND_BATH_FIELD = "bed_and_bath"
SQFT_FIELD = "sqft"
LAST_MOVE_IN_FIELD = "last_move_in"
LAST_MOVE_OUT_FIELD = "last_move_out"

# VacantUnit attribute -> GHL custom_objects.tmpl_properties field key
# (confirmed from the merge-field tags the user shared).
_FIELD_MAP: dict[str, str] = {
    "bed_bath": "bedbath",
    "days_vacant": "days_vacant",
    "last_move_in": "last_move_in",
    "last_move_out": "last_move_out",
    "sqft": "lot_size",
    "occupancy_status": "occupancy_status",
    "property_name": "property_name",
    "advertised_rent": "rent_price",
    "street_address": "street_address",
    "unit": "unit",
    "unit_id": "unit_id",
}


@dataclass
class VacantUnit:
    unit_id: str
    property_name: str
    street_address: str
    unit: str
    occupancy_status: str
    days_vacant: Optional[str]
    advertised_rent: Optional[str]
    bed_bath: Optional[str]
    sqft: Optional[str]
    last_move_in: Optional[str]
    last_move_out: Optional[str]


def vacancy_key(row: dict[str, Any]) -> str:
    """Stable per-unit idempotency key from AppFolio's `unit_id`."""
    unit_id = row.get(UNIT_ID_FIELD)
    if unit_id:
        return str(unit_id)
    raise ValueError(f"Vacancy row missing '{UNIT_ID_FIELD}': {row}")


def from_appfolio_row(row: dict[str, Any]) -> VacantUnit:
    return VacantUnit(
        unit_id=vacancy_key(row),
        property_name=(row.get(UNIT_FIELD) or "").strip(),
        street_address=(row.get(ADDRESS_FIELD) or "").strip(),
        unit=(row.get(UNIT_FIELD) or "").strip(),
        occupancy_status=(row.get(UNIT_STATUS_FIELD) or "").strip(),
        days_vacant=row.get(DAYS_VACANT_FIELD),
        advertised_rent=row.get(ADVERTISED_RENT_FIELD),
        bed_bath=row.get(BED_AND_BATH_FIELD),
        sqft=row.get(SQFT_FIELD),
        last_move_in=row.get(LAST_MOVE_IN_FIELD),
        last_move_out=row.get(LAST_MOVE_OUT_FIELD),
    )


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else None


def _as_iso_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()

    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        for pattern in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(text, pattern).date().isoformat()
            except ValueError:
                continue
    return None


def _as_money(value: Any) -> Optional[dict[str, Any]]:
    """GHL monetary field requires {currency, value} with key 'value' (not
    'amount'). Currency 'default' uses the account's configured currency."""
    if value in (None, ""):
        return None
    try:
        amount = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return {"currency": "default", "value": amount}


def to_ghl_custom_object_payload(unit: VacantUnit) -> dict[str, Any]:
    """Builds the GHL custom object record's `properties` payload, dropping
    unset values (GHL treats absent and null differently for some fields)."""
    values = {
        "bed_bath": unit.bed_bath,
        "sqft": _as_int(unit.sqft),
        "occupancy_status": unit.occupancy_status,
        "property_name": unit.property_name,
        "advertised_rent": _as_money(unit.advertised_rent),
        "street_address": unit.street_address,
        "unit": unit.unit,
        "unit_id": _as_int(unit.unit_id),
        "days_vacant": _as_int(unit.days_vacant),
        "last_move_in": _as_iso_date(unit.last_move_in),
        "last_move_out": _as_iso_date(unit.last_move_out),
    }
    return {
        _FIELD_MAP[attr]: value
        for attr, value in values.items()
        if value not in (None, "") and attr in _FIELD_MAP
    }
