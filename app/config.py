"""Loads and validates configuration from environment variables / .env file."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_REQUIRED_VARS = [
    "APPFOLIO_CLIENT_ID",
    "APPFOLIO_CLIENT_SECRET",
    "APPFOLIO_DATABASE",
    "GHL_PRIVATE_TOKEN",
    "GHL_LOCATION_ID",
]


@dataclass(frozen=True)
class Config:
    appfolio_client_id: str
    appfolio_client_secret: str
    appfolio_database: str
    appfolio_guest_card_report: str
    appfolio_vacancy_report: str
    ghl_private_token: str
    ghl_location_id: str
    ghl_property_object_key: str
    state_db_path: str
    initial_lookback_days: int
    ghl_ooo_calendar_id: str
    ghl_leasing_pipeline_name: str

    @property
    def appfolio_reports_base_url(self) -> str:
        return f"https://{self.appfolio_database}.appfolio.com/api/v2/reports"


def load_config() -> Config:
    missing = [name for name in _REQUIRED_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )

    return Config(
        appfolio_client_id=os.environ["APPFOLIO_CLIENT_ID"],
        appfolio_client_secret=os.environ["APPFOLIO_CLIENT_SECRET"],
        appfolio_database=os.environ["APPFOLIO_DATABASE"],
        appfolio_guest_card_report=os.getenv("APPFOLIO_GUEST_CARD_REPORT", "guest_card_inquiries"),
        appfolio_vacancy_report=os.getenv("APPFOLIO_VACANCY_REPORT", "unit_vacancy"),
        ghl_private_token=os.environ["GHL_PRIVATE_TOKEN"],
        ghl_location_id=os.environ["GHL_LOCATION_ID"],
        # Custom Object key for the GHL "Property" object vacancy records sync into;
        # required only when running the vacancy sync, not the guest card sync.
        ghl_property_object_key=os.getenv("GHL_PROPERTY_OBJECT_KEY", "custom_objects.tmpl_properties"),
        state_db_path=os.getenv("STATE_DB_PATH", "./data/state.db"),
        initial_lookback_days=int(os.getenv("INITIAL_LOOKBACK_DAYS", "1")),
        # Optional: GHL calendar (synced from a company Google Calendar) where an
        # event titled with a team member's first name marks them out of office
        # for that day. If unset, the out-of-office check is skipped entirely.
        ghl_ooo_calendar_id=os.getenv("GHL_OOO_CALENDAR_ID", ""),
        ghl_leasing_pipeline_name=os.getenv("GHL_LEASING_PIPELINE_NAME", "Leasing Pipeline"),
    )
