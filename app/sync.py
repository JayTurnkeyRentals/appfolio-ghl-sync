"""Orchestrates one sync cycle: fetch guest card INQUIRIES from AppFolio and
create the corresponding contact + note in GoHighLevel.

Data model: `guest_card_id` identifies a PERSON (AppFolio's guest card); one
person can have many inquiries (interest in different units, at different
times), and AppFolio's report returns one row per inquiry -- multiple rows
can share the same guest_card_id. So:
    guest_card_id  -> one GHL contact (created once, reused forever)
    each inquiry   -> one GHL note (tracked independently via mapper.inquiry_id)

Sync state is tracked per INQUIRY (see state_store.synced_inquiries), not per
guest_card_id -- treating guest_card_id as the idempotency key was the
original bug: it caused every inquiry after a person's first to be silently
skipped forever, since the person (not the inquiry) was already marked synced.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from app import mapper
from app.appfolio_client import AppFolioClient
from app.config import Config
from app.ghl_client import GHLApiError, GHLClient
from app.state_store import StateStore

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y-%m-%d"


def _first_name_normalized(name: Optional[str]) -> str:
    normalized = mapper.normalize_user_name(name) or ""
    return normalized.split(" ", 1)[0] if normalized else ""


def _out_of_office_first_names(
    ghl: GHLClient,
    config: Config,
    ooo_cache: dict,
    on_date: Optional[date],
) -> frozenset[str]:
    """Normalized first names marked out-of-office on `on_date`, per the
    configured GHL calendar (an event titled with just a first name means
    that person is out that day). Cached per-date since the same day is
    often checked for multiple guest cards in one sync run."""
    if not config.ghl_ooo_calendar_id or on_date is None:
        return frozenset()

    if on_date not in ooo_cache:
        start = datetime.combine(on_date, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        try:
            events = ghl.list_calendar_events(
                config.ghl_ooo_calendar_id,
                int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
            )
        except GHLApiError as e:
            logger.warning("Failed to fetch OOO calendar events for %s: %s", on_date, e)
            events = []
        names = frozenset(
            _first_name_normalized(event.get("title")) for event in events if event.get("title")
        )
        ooo_cache[on_date] = {n for n in names if n}

    return ooo_cache[on_date]


def _is_agent_out_of_office(agent_name: str, ooo_first_names: frozenset[str]) -> bool:
    return bool(ooo_first_names) and _first_name_normalized(agent_name) in ooo_first_names


def _resolve_leasing_pipeline(ghl: GHLClient, config: Config) -> Optional[dict]:
    """Finds the configured Leasing Pipeline and its first stage, used to
    auto-create an opportunity for every new contact. Returns None (logging a
    warning) if the pipeline can't be found -- opportunity creation is then
    skipped entirely rather than failing the whole sync."""
    try:
        pipelines = ghl.list_pipelines()
    except GHLApiError as e:
        logger.warning("Failed to fetch GHL pipelines: %s. Opportunity creation will be skipped.", e)
        return None

    target = config.ghl_leasing_pipeline_name.strip().lower()
    for pipeline in pipelines:
        if (pipeline.get("name") or "").strip().lower() == target:
            stages = pipeline.get("stages") or []
            if not stages:
                logger.warning("Pipeline '%s' has no stages; skipping opportunity creation.", pipeline.get("name"))
                return None
            return {"pipeline_id": pipeline["id"], "stage_id": stages[0]["id"]}

    logger.warning(
        "Could not find a GHL pipeline named '%s' (check GHL_LEASING_PIPELINE_NAME); "
        "opportunity creation will be skipped.", config.ghl_leasing_pipeline_name,
    )
    return None


def _build_user_map(ghl: GHLClient) -> dict[str, str]:
    """Maps normalized GHL user name -> GHL user id.

    If the user list can't be fetched (e.g., due to token scope issues),
    returns an empty dict and logs a warning.
    """
    try:
        users = ghl.list_users()
    except GHLApiError as e:
        logger.warning("Failed to fetch GHL users: %s. User assignment will be skipped.", e)
        return {}

    mapping: dict[str, str] = {}
    for user in users:
        name = user.get("name") or f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
        normalized = mapper.normalize_user_name(name)
        if normalized:
            mapping[normalized] = user["id"]
    return mapping


def _get_existing_notes(ghl: GHLClient, contact_id: str) -> List[dict]:
    try:
        return ghl.list_notes(contact_id)
    except GHLApiError as e:
        logger.warning("Failed to fetch notes for contact %s: %s", contact_id, e)
        return []


def _next_round_robin_agent(state: StateStore, ooo_first_names: frozenset[str] = frozenset()) -> Optional[str]:
    """Advances and returns the next agent in the rotation, skipping anyone
    marked out-of-office for the day (see _out_of_office_first_names). Skipped
    agents keep their rotation position -- only the agent actually chosen
    becomes the new "last assigned" pointer.

    Only call this when you are actually about to assign an agent -- it has
    the side effect of advancing the rotation pointer, so calling it and then
    not using the result would skip that agent's fair turn next time.
    """
    agents = state.list_round_robin_agents(active_only=True)
    if not agents:
        return None

    last = state.get_last_round_robin_agent()
    if last not in agents:
        start_index = 0
    else:
        start_index = (agents.index(last) + 1) % len(agents)

    ordered = agents[start_index:] + agents[:start_index]
    for candidate in ordered:
        if _is_agent_out_of_office(candidate, ooo_first_names):
            logger.info("Round robin: skipping '%s', marked out of office today", candidate)
            continue
        state.set_last_round_robin_agent(candidate)
        return candidate

    logger.warning("Round robin: all active agents are out of office today; leaving contact unassigned")
    return None


def _maybe_round_robin_assign_existing_contact(
    state: StateStore,
    ghl: GHLClient,
    user_map: dict[str, str],
    contact_id: str,
    ooo_first_names: frozenset[str] = frozenset(),
) -> None:
    """For a contact that already exists (repeat inquiry / grouped case):
    if round robin is enabled AND the contact has no agent assigned yet,
    assign the next agent in rotation (skipping anyone out of office today).
    If the contact already has an agent assigned, leave it alone -- this is
    the "skip assignment" rule."""
    if not state.is_round_robin_enabled():
        return

    try:
        contact = ghl.get_contact(contact_id)
    except GHLApiError as e:
        logger.warning("Could not fetch contact %s to check round robin assignment: %s", contact_id, e)
        return

    if contact.get("assignedTo"):
        logger.debug("Contact %s already has an assigned agent; skipping round robin assignment", contact_id)
        return

    agent_name = _next_round_robin_agent(state, ooo_first_names)
    if not agent_name:
        logger.warning("Round robin is enabled but no active agents are configured")
        return

    ghl_user_id = user_map.get(mapper.normalize_user_name(agent_name))
    if not ghl_user_id:
        logger.warning(
            "Round robin agent '%s' has no matching GHL user (check spelling in the roster "
            "vs. their name in GHL); skipping assignment for contact %s", agent_name, contact_id,
        )
        return

    try:
        ghl.update_contact(contact_id, {"assignedTo": ghl_user_id})
        logger.info("Round robin: assigned '%s' to contact %s", agent_name, contact_id)
    except GHLApiError as e:
        logger.warning("Failed to round-robin assign contact %s: %s", contact_id, e)


def _resolve_contact_id(
    state: StateStore,
    ghl: GHLClient,
    user_map: dict[str, str],
    gc_id: str,
    guest_card: dict,
    now: str,
    ooo_first_names: frozenset[str] = frozenset(),
) -> tuple[str, bool]:
    """Find or create the GHL contact for this guest_card_id (person).

    Returns (contact_id, was_newly_created).

    Resolution order:
      1. Local state, keyed by "gc:{guest_card_id}" -- the fast, common path
         once a person has been seen before.
      2. Local state, keyed by email/phone -- catches the rare case where
         AppFolio created a second guest_card_id for the same real person.
      3. Create the contact in GHL. If GHL reports a duplicate (its own
         server-side email/phone match -- more reliable than us guessing),
         recover the existing contact id from the error body instead of
         treating it as a failure.

    Agent assignment: on a BRAND NEW contact, the agent (round robin or
    AppFolio's assigned_user, depending on config) is set directly in the
    create payload. If that agent is out of office (see ooo_first_names),
    the roster's next round-robin agent is used instead, regardless of
    whether round robin is the normal assignment mode. On an EXISTING
    contact (cases 1, 2, and the duplicate recovery in case 3), round robin
    only assigns if the contact doesn't already have an agent -- see
    _maybe_round_robin_assign_existing_contact.
    """
    primary_key = f"gc:{gc_id}"
    contact_id = state.get_contact_by_key(primary_key)
    if contact_id:
        _maybe_round_robin_assign_existing_contact(state, ghl, user_map, contact_id, ooo_first_names)
        return contact_id, False

    secondary_key = mapper.contact_dedup_key(guest_card)
    if secondary_key:
        contact_id = state.get_contact_by_key(secondary_key)
        if contact_id:
            # Backfill the primary key so future lookups for this gc_id are direct.
            state.upsert_contact_key(primary_key, contact_id, now)
            _maybe_round_robin_assign_existing_contact(state, ghl, user_map, contact_id, ooo_first_names)
            return contact_id, False

    contact_payload = mapper.guest_card_to_contact_payload(guest_card)

    agent_name: Optional[str] = None
    if state.is_round_robin_enabled():
        agent_name = _next_round_robin_agent(state, ooo_first_names)
        if not agent_name:
            logger.warning("Round robin is enabled but no active agents are configured")
    else:
        assigned_user = guest_card.get(mapper.ASSIGNED_USER_FIELD)
        if assigned_user and _is_agent_out_of_office(assigned_user, ooo_first_names):
            logger.info(
                "Guest card %s: assigned user '%s' is out of office today; "
                "reassigning via round robin roster instead", gc_id, assigned_user,
            )
            agent_name = _next_round_robin_agent(state, ooo_first_names)
        else:
            agent_name = assigned_user

    if agent_name:
        ghl_user_id = user_map.get(mapper.normalize_user_name(agent_name))
        if ghl_user_id:
            contact_payload["assignedTo"] = ghl_user_id
        else:
            logger.warning(
                "Agent '%s' has no matching GHL user (check spelling); "
                "new contact for guest card %s will be created unassigned", agent_name, gc_id,
            )

    try:
        result = ghl.create_contact(contact_payload)
        contact_id = result["contact"]["id"]
        was_new = True
    except GHLApiError as e:
        if e.status_code == 400 and "duplicated contacts" in str(e.body):
            meta = e.body.get("meta", {}) if isinstance(e.body, dict) else {}
            contact_id = meta.get("contactId")
            if not contact_id:
                logger.error("Could not extract contact ID from duplicate error: %s", e.body)
                raise
            was_new = False
            logger.info("Guest card %s: contact already exists in GHL (%s), found via duplicate error", gc_id, contact_id)
            # This contact already existed in GHL even though it wasn't in our
            # local cache yet -- still subject to the "skip if already assigned" rule.
            _maybe_round_robin_assign_existing_contact(state, ghl, user_map, contact_id, ooo_first_names)
        else:
            raise

    state.upsert_contact_key(primary_key, contact_id, now)
    if secondary_key:
        state.upsert_contact_key(secondary_key, contact_id, now)

    return contact_id, was_new


def _maybe_create_leasing_opportunity(
    ghl: GHLClient,
    pipeline_info: Optional[dict],
    contact_id: str,
    guest_card: dict,
) -> None:
    """Ensures the contact has an opportunity in the Leasing Pipeline, so every
    lead's status can be tracked. Checks GHL first (rather than relying on
    contact_was_new) so contacts that already existed before this pipeline was
    configured, or that lost their opportunity, still get one."""
    if not pipeline_info:
        return

    try:
        existing = ghl.search_opportunities(contact_id, pipeline_info["pipeline_id"])
    except GHLApiError as e:
        logger.warning("Failed to check existing opportunities for contact %s: %s", contact_id, e)
        return
    if existing:
        return

    name = mapper.full_name(guest_card) or "AppFolio Lead"
    payload = {
        "pipelineId": pipeline_info["pipeline_id"],
        "pipelineStageId": pipeline_info["stage_id"],
        "name": name,
        "status": "open",
        "contactId": contact_id,
    }
    try:
        ghl.create_opportunity(payload)
    except GHLApiError as e:
        logger.warning("Failed to create leasing opportunity for contact %s: %s", contact_id, e)


def _sync_inquiry(
    state: StateStore,
    ghl: GHLClient,
    user_map: dict[str, str],
    guest_card: dict,
    config: Config,
    ooo_cache: dict,
    pipeline_info: Optional[dict],
) -> str:
    """Sync a single inquiry row. Returns one of: 'new', 'grouped', 'repaired', 'duplicate'."""
    gc_id = mapper.guest_card_id(guest_card)
    inquiry_key = mapper.inquiry_id(guest_card)
    now = datetime.now(timezone.utc).isoformat()

    existing = state.get_synced_inquiry(inquiry_key)
    if existing is not None and existing["ghl_note_id"] is not None:
        return "skipped"

    contact_id = existing["ghl_contact_id"] if existing is not None else None
    contact_was_new = False
    if not contact_id:
        ooo_first_names = _out_of_office_first_names(ghl, config, ooo_cache, mapper.received_date(guest_card))
        contact_id, contact_was_new = _resolve_contact_id(
            state, ghl, user_map, gc_id, guest_card, now, ooo_first_names
        )

    inquiry_address = mapper.inquiry_address(guest_card)
    if inquiry_address and not contact_was_new:
        try:
            ghl.update_contact(contact_id, {"address1": inquiry_address})
        except GHLApiError as e:
            logger.warning(
                "Failed to update contact %s address from inquiry %s: %s",
                contact_id, inquiry_key, e,
            )

    # Runs for every inquiry (not just brand new contacts) so contacts that
    # already existed before the leasing pipeline was configured -- or that
    # lost their opportunity -- still get one applied.
    _maybe_create_leasing_opportunity(ghl, pipeline_info, contact_id, guest_card)

    note_body, note_title = mapper.guest_card_to_note(guest_card, inquiry_key)

    # Safety net: if a previous run created the note but crashed before
    # writing state, don't create a second one -- check for the exact
    # per-inquiry marker among the contact's existing notes.
    existing_notes = _get_existing_notes(ghl, contact_id)
    marker = mapper.note_dedup_marker(inquiry_key)
    note_id = None
    was_duplicate = False
    for note in existing_notes:
        if marker in note.get("body", ""):
            note_id = note.get("id")
            was_duplicate = True
            break

    if note_id is None:
        note_result = ghl.create_note(contact_id, note_body, note_title)
        note_id = note_result.get("note", {}).get("id")

    state.upsert_synced_inquiry(inquiry_key, gc_id, contact_id, note_id, now)

    if was_duplicate:
        return "duplicate"
    if existing is not None:
        return "repaired"
    if contact_was_new:
        return "new"
    return "grouped"


def _find_existing_contact_id(
    state: StateStore,
    ghl: GHLClient,
    guest_card: dict,
    inquiry_key: str,
    now: str,
) -> Optional[str]:
    """Find an existing contact without creating one during a resync."""
    gc_id = mapper.guest_card_id(guest_card)
    primary_key = f"gc:{gc_id}"
    secondary_key = mapper.contact_dedup_key(guest_card)
    existing = state.get_synced_inquiry(inquiry_key)
    contact_id = existing["ghl_contact_id"] if existing is not None else None
    if not contact_id:
        contact_id = state.get_contact_by_key(primary_key)
    if not contact_id and secondary_key:
        contact_id = state.get_contact_by_key(secondary_key)

    if not contact_id:
        email = (guest_card.get(mapper.EMAIL_FIELD) or "").strip().lower()
        phone = "".join(character for character in str(guest_card.get(mapper.PHONE_FIELD) or "") if character.isdigit())
        searches = []
        if email:
            searches.append(("email", email))
        if phone:
            searches.append(("phone", phone))

        for search_type, value in searches:
            candidates = ghl.search_contacts(**{search_type: value})
            if search_type == "email":
                matches = [
                    candidate for candidate in candidates
                    if (candidate.get("email") or "").strip().lower() == value
                ]
            else:
                matches = [
                    candidate for candidate in candidates
                    if "".join(
                        character
                        for character in str(candidate.get("phone") or "")
                        if character.isdigit()
                    ) == value
                ]
            if len(matches) == 1:
                contact_id = matches[0].get("id")
                if contact_id:
                    break
            if len(matches) > 1:
                logger.warning(
                    "Resync found multiple GHL contacts matching %s for guest card %s; "
                    "leaving them unchanged", search_type, gc_id,
                )
                return None

    if not contact_id:
        logger.warning(
            "Resync could not find an existing GHL contact for guest card %s; "
            "not creating a contact", gc_id,
        )
        return None

    state.upsert_contact_key(primary_key, contact_id, now)
    if secondary_key:
        state.upsert_contact_key(secondary_key, contact_id, now)
    return contact_id


def resync_inquiry(
    state: StateStore,
    ghl: GHLClient,
    user_map: dict[str, str],
    guest_card: dict,
) -> str:
    """Refresh one existing contact, its AppFolio owner, and its inquiry note.

    Returns 'updated', 'created_note', or 'missing_contact'. Existing notes
    are updated in place using their inquiry marker, so reruns don't duplicate
    notes and older notes gain the current preference fields.
    """
    gc_id = mapper.guest_card_id(guest_card)
    inquiry_key = mapper.inquiry_id(guest_card)
    now = datetime.now(timezone.utc).isoformat()
    contact_id = _find_existing_contact_id(state, ghl, guest_card, inquiry_key, now)
    if not contact_id:
        return "missing_contact"

    contact_payload = mapper.guest_card_to_contact_payload(guest_card)
    contact_payload.pop("tags", None)

    assigned_user = guest_card.get(mapper.ASSIGNED_USER_FIELD)
    if assigned_user:
        ghl_user_id = user_map.get(mapper.normalize_user_name(assigned_user))
        if ghl_user_id:
            contact_payload["assignedTo"] = ghl_user_id
        else:
            logger.warning(
                "AppFolio assigned user '%s' has no matching GHL user; "
                "preserving current assignment for contact %s",
                assigned_user, contact_id,
            )

    ghl.update_contact(contact_id, contact_payload)

    note_body, note_title = mapper.guest_card_to_note(guest_card, inquiry_key)
    marker = mapper.note_dedup_marker(inquiry_key)
    existing_notes = ghl.list_notes(contact_id)
    matching_note = next(
        (note for note in existing_notes if marker in (note.get("body") or "")),
        None,
    )
    if matching_note:
        note_id = matching_note.get("id")
        if not note_id:
            raise RuntimeError(
                f"Matched inquiry note for contact {contact_id} has no note ID"
            )
        if matching_note.get("body") != note_body:
            ghl.update_note(contact_id, note_id, note_body, note_title)
        outcome = "updated"
    else:
        note_result = ghl.create_note(contact_id, note_body, note_title)
        note_id = note_result.get("note", {}).get("id")
        outcome = "created_note"

    state.upsert_synced_inquiry(inquiry_key, gc_id, contact_id, note_id, now)
    return outcome


def run_sync(config: Config) -> None:
    state = StateStore(config.state_db_path)
    appfolio = AppFolioClient(config)
    ghl = GHLClient(config)

    received_on_from = (date.today() - timedelta(days=5)).strftime(DATE_FORMAT)
    received_on_to = date.today().strftime(DATE_FORMAT)

    logger.info("===== SYNC START =====")
    logger.info("Fetching guest cards from %s to %s", received_on_from, received_on_to)

    guest_cards = appfolio.fetch_guest_cards(received_on_from, received_on_to)
    logger.info("Fetched %d guest card inquiry row(s) total", len(guest_cards))

    user_map = _build_user_map(ghl)
    logger.info("Loaded %d GHL user(s) for assignment matching", len(user_map))

    ooo_cache: dict = {}
    pipeline_info = _resolve_leasing_pipeline(ghl, config)

    counts = {"new": 0, "grouped": 0, "repaired": 0, "duplicate": 0, "skipped": 0}
    errors = 0

    for guest_card in guest_cards:
        try:
            gc_id = mapper.guest_card_id(guest_card)
        except ValueError:
            logger.exception("Skipping inquiry row with no guest_card_id")
            errors += 1
            continue

        try:
            inquiry_key = mapper.inquiry_id(guest_card)
        except ValueError:
            logger.exception("Skipping inquiry for guest card %s with no received timestamp", gc_id)
            errors += 1
            continue

        try:
            outcome = _sync_inquiry(state, ghl, user_map, guest_card, config, ooo_cache, pipeline_info)
            counts[outcome] += 1
            if outcome != "skipped":
                logger.info("Guest card %s, inquiry %s -> %s", gc_id, inquiry_key, outcome)
        except GHLApiError:
            logger.exception("GHL API error syncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1
        except Exception:
            logger.exception("Unexpected error syncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1

    if errors == 0:
        state.set_watermark(received_on_to)
        logger.info(
            "Sync complete: %d new contact(s), %d grouped (added note to existing contact), "
            "%d repaired (retried a previously-failed note), %d duplicate(s) skipped, "
            "%d already synced, watermark advanced to %s",
            counts["new"], counts["grouped"], counts["repaired"], counts["duplicate"],
            counts["skipped"], received_on_to,
        )
    else:
        logger.warning(
            "Sync finished with %d error(s) out of %d inquiry row(s); watermark NOT advanced",
            errors, len(guest_cards),
        )
        raise RuntimeError(f"Sync completed with {errors} error(s)")
