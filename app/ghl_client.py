"""Client for the GoHighLevel (LeadConnector) v3 Contacts/Notes API."""
import logging
import re
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import Config

logger = logging.getLogger(__name__)

BASE_URL = "https://services.leadconnectorhq.com"


class GHLApiError(Exception):
    """Raised when the GHL API returns an error response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GHL API error {status_code}: {body}")


class GHLClient:
    def __init__(self, config: Config):
        self._config = config
        self._session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "PUT"),
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retries))
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.ghl_private_token}",
                "Version": "v3",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        response = self._session.request(
            method,
            f"{BASE_URL}{path}",
            json=json_body,
            params=params,
            headers=headers,
            timeout=30,
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json()

    def create_contact(self, payload: dict) -> dict:
        payload = {**payload, "locationId": self._config.ghl_location_id}
        result = self._request("POST", "/contacts/", payload)
        logger.info("Created GHL contact %s", result.get("contact", {}).get("id"))
        return result

    def get_contact(self, contact_id: str) -> dict:
        """Fetch a contact's current state (used to check e.g. assignedTo
        before round-robin assigning, so we don't clobber an existing owner)."""
        result = self._request("GET", f"/contacts/{contact_id}")
        return result.get("contact", {})

    def update_contact(self, contact_id: str, payload: dict) -> dict:
        result = self._request("PUT", f"/contacts/{contact_id}", payload)
        logger.info("Updated GHL contact %s", contact_id)
        return result

    def create_note(self, contact_id: str, body: str, title: str | None = None) -> dict:
        payload: dict[str, Any] = {"body": body}
        if title:
            payload["title"] = title
        result = self._request("POST", f"/contacts/{contact_id}/notes", payload)
        logger.info("Created note on GHL contact %s", contact_id)
        return result

    def update_note(self, contact_id: str, note_id: str, body: str, title: str | None = None) -> dict:
        payload: dict[str, Any] = {"body": body}
        if title:
            payload["title"] = title
        result = self._request("PUT", f"/contacts/{contact_id}/notes/{note_id}", payload)
        logger.info("Updated note %s on GHL contact %s", note_id, contact_id)
        return result

    def list_users(self) -> list[dict]:
        """Returns all users for the configured location. Uses the legacy
        `/users/` endpoint (Version 2021-07-28) since v3 has no list/search-by-location
        route for sub-account tokens."""
        response = self._session.get(
            f"{BASE_URL}/users/",
            params={"locationId": self._config.ghl_location_id},
            headers={"Version": "2021-07-28"},
            timeout=30,
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json().get("users", [])

    def search_contacts(self, email: Optional[str] = None, phone: Optional[str] = None) -> list[dict]:
        """Search for contacts by email or phone using the list endpoint's
        free-text `query` param.

        Note: GHL's GET /contacts/ list endpoint validates its query params
        strictly and has no dedicated `phone` (or `email`) property -- passing
        one raises a 422 "property phone should not exist". Only `query`
        (free text), `locationId`, `limit`, etc. are accepted, so both email
        and phone lookups go through `query`.
        """
        if not email and not phone:
            return []

        # Build query parameters for the list endpoint
        params = {
            "locationId": self._config.ghl_location_id,
            "limit": 10,  # We only need the first match
        }
        if email:
            params["query"] = email.strip().lower()
        elif phone:
            params["query"] = re.sub(r"\D", "", phone)
        
        try:
            # First try the list endpoint (more permissive)
            response = self._session.get(
                f"{BASE_URL}/contacts/",
                params=params,
                timeout=30
            )
            
            if response.status_code == 404:
                return []
            if response.status_code == 401:
                # If list also fails with 401, fall back to lookup as a last resort
                logger.warning("List endpoint 401, trying lookup endpoint")
                return self._search_lookup(email, phone)
            if not response.ok:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                raise GHLApiError(response.status_code, body)
            
            return response.json().get("contacts", [])
            
        except requests.RequestException as e:
            logger.warning("Error searching GHL contacts via list: %s", e)
            return []

    def _search_lookup(self, email: Optional[str], phone: Optional[str]) -> list[dict]:
        """Fallback using the lookup endpoint (may require extra scopes)."""
        if not email and not phone:
            return []
        
        params = {"locationId": self._config.ghl_location_id}
        if email:
            params["email"] = email.strip().lower()
        if phone:
            params["phone"] = re.sub(r"\D", "", phone)
        
        try:
            response = self._session.get(
                f"{BASE_URL}/contacts/lookup",
                params=params,
                timeout=30
            )
            if response.status_code == 404:
                return []
            if not response.ok:
                # Don't raise, just return empty
                logger.warning("Lookup endpoint failed with %s", response.status_code)
                return []
            return response.json().get("contacts", [])
        except Exception as e:
            logger.warning("Lookup fallback error: %s", e)
            return []

    def list_notes(self, contact_id: str) -> list[dict]:
        """Retrieve notes for a given contact.

        Args:
            contact_id: The GHL contact ID.

        Returns:
            List of note dicts, each containing at least 'id' and 'body'.
        """
        response = self._session.get(
            f"{BASE_URL}/contacts/{contact_id}/notes",
            timeout=30
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json().get("notes", [])

    def list_calendar_events(self, calendar_id: str, start_time_ms: int, end_time_ms: int) -> list[dict]:
        """Fetch events on the given calendar within [start_time_ms, end_time_ms)
        (epoch milliseconds), used to detect out-of-office days."""
        response = self._session.get(
            f"{BASE_URL}/calendars/events",
            params={
                "locationId": self._config.ghl_location_id,
                "calendarId": calendar_id,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
            },
            timeout=30,
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json().get("events", [])

    def list_pipelines(self) -> list[dict]:
        response = self._session.get(
            f"{BASE_URL}/opportunities/pipelines",
            params={"locationId": self._config.ghl_location_id},
            timeout=30,
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json().get("pipelines", [])

    def create_opportunity(self, payload: dict) -> dict:
        payload = {**payload, "locationId": self._config.ghl_location_id}
        result = self._request("POST", "/opportunities/", payload)
        logger.info("Created GHL opportunity %s", result.get("opportunity", {}).get("id"))
        return result

    def search_opportunities(self, contact_id: str, pipeline_id: Optional[str] = None) -> list[dict]:
        """Search opportunities for a contact, optionally scoped to a pipeline."""
        params = {"locationId": self._config.ghl_location_id, "contactId": contact_id}
        if pipeline_id:
            params["pipelineId"] = pipeline_id
        response = self._session.get(f"{BASE_URL}/opportunities/search", params=params, timeout=30)
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise GHLApiError(response.status_code, body)
        return response.json().get("opportunities", [])

    # ---- custom objects (e.g. "Property" vacancy records) -----------------
    # Body shapes below are best-effort pending confirmation against a live
    # test call -- adjust once verified against the actual GHL custom object.

    def search_custom_object_records(
        self, object_key: str, filters: Optional[dict] = None, page_limit: int = 20
    ) -> list[dict]:
        body: dict[str, Any] = {
            "locationId": self._config.ghl_location_id,
            "page": 1,
            "pageLimit": page_limit,
        }
        if filters:
            body.update(filters)
        result = self._request("POST", f"/objects/{object_key}/records/search", body)
        return result.get("records", [])

    def get_custom_object_schema(self, object_key: str) -> dict:
        result = self._request(
            "GET",
            f"/objects/{object_key}",
            params={"locationId": self._config.ghl_location_id},
            headers={"Version": "2021-07-28"},
        )
        return result.get("object", {})

    def create_custom_object_record(self, object_key: str, properties: dict) -> dict:
        payload = {"locationId": self._config.ghl_location_id, "properties": properties}
        result = self._request("POST", f"/objects/{object_key}/records", payload)
        logger.info("Created GHL custom object record for '%s'", object_key)
        return result

    def update_custom_object_record(self, object_key: str, record_id: str, properties: dict) -> dict:
        payload = {"properties": properties}
        result = self._request(
            "PUT",
            f"/objects/{object_key}/records/{record_id}",
            payload,
            params={"locationId": self._config.ghl_location_id},
        )
        logger.info("Updated GHL custom object record %s for '%s'", record_id, object_key)
        return result
