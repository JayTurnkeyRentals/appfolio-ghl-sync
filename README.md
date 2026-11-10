# AppFolio to GoHighLevel Sync

A Python service that synchronizes leasing data from AppFolio Property Manager into GoHighLevel (GHL). It runs unattended on a Debian 13 VPS and performs two independent jobs:

1. **Guest card sync.** Pulls guest card inquiries from AppFolio and creates or updates the corresponding GHL contact, inquiry note, and Leasing Pipeline opportunity. New leads are assigned to a leasing agent, either through a local round-robin roster or through the user assigned in AppFolio.
2. **Vacancy sync.** Pulls currently leasable vacant units from AppFolio and upserts each one as a record in a GHL "Property" custom object. Units that stop qualifying are marked "Unavailable".

AppFolio's Reports API is pull-only, so the service polls on a schedule (systemd timers) and records what it has already processed in a local SQLite database.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Repository Layout](#repository-layout)
3. [Script Reference](#script-reference)
4. [Configuration](#configuration)
5. [Deployment on Debian 13](#deployment-on-debian-13)
6. [Scheduling with systemd](#scheduling-with-systemd)
7. [Operations and Maintenance](#operations-and-maintenance)
8. [Local State Database](#local-state-database)
9. [Known Limitations and Maintenance Notes](#known-limitations-and-maintenance-notes)

---

## How It Works

### Guest card sync

AppFolio's guest card report returns one row per inquiry. A single person (identified by `guest_card_id`) can appear in many rows if they inquire about multiple units over time. The sync models this as follows:

| AppFolio | GoHighLevel |
|---|---|
| Guest card (`guest_card_id`) | One contact, created once and reused |
| Inquiry (one report row) | One note on that contact |
| First inquiry for a contact | One opportunity in the Leasing Pipeline (first stage) |

Each run fetches inquiries received in a rolling five-day window ending today. For every row the sync:

1. Skips the inquiry if it has already been recorded as synced.
2. Resolves the GHL contact, in this order: local state by `guest_card_id`, local state by email or phone, then creation in GHL. If GHL rejects the create as a duplicate, the existing contact ID is recovered from the error response.
3. Updates the contact address from the inquiry property when the contact already existed.
4. Ensures the contact has an opportunity in the configured Leasing Pipeline.
5. Creates an inquiry note containing property, unit, status, lead type, move-in preference, max rent, bed/bath preference, pet preference, income, credit score, received date, and comments. Each note ends with a hidden dedup marker (`[inq:<hash>]`) so a run that crashed after creating a note will not create a second one.
6. Records the result in the local state database.

If any inquiry in a run fails, the run exits with a non-zero status and the watermark is not advanced. Because the fetch window is a rolling five days and processing is idempotent, the next run retries safely.

### Agent assignment

Assignment is applied when a contact is created, and in some cases to existing contacts that have no agent yet.

- **Round robin enabled.** The next active agent in the roster is assigned. Agents marked out of office for the inquiry date are skipped without losing their position in the rotation. Existing contacts that already have an assigned agent are never reassigned.
- **Round robin disabled.** The agent named in AppFolio's `assigned_user` field is assigned, matched by name against GHL users. If that agent is out of office, the roster is used as a fallback.
- **Out-of-office detection.** If `GHL_OOO_CALENDAR_ID` is set, the sync reads that GHL calendar for the inquiry date. An event whose title is a team member's first name marks that person as out of office for that day. If the variable is not set, the check is skipped.

Agent names in the roster must match the corresponding GHL user names (case and parenthetical suffixes such as "(Hidden)" are ignored during matching).

### Vacancy sync

Each run fetches the AppFolio unit vacancy report and keeps only units that meet all of the following rules (applied client-side, because the AppFolio API has no exclusion filters):

- Unit status is exactly `Vacant-Unrented`.
- Property name does not contain the whole words "property", "properties", "HOA", or "POA" (case-insensitive).
- Unit tags do not include `End Management`.

Each qualifying unit is upserted into the GHL custom object identified by `GHL_PROPERTY_OBJECT_KEY`, keyed by AppFolio `unit_id`. Records are updated on every run because values such as days vacant change daily. If a saved GHL record has been deleted, it is recreated. After a fully successful pass, any previously synced unit that no longer qualifies is updated with `occupancy_status = "Unavailable"`. This reconciliation is skipped if any unit failed during the run.

---

## Repository Layout

```
appfolio-ghl-sync/
  main.py                          Entry point: guest card sync
  full_resync.py                   Backfill over a wide date range
  reset_state.py                   Clear local sync state
  inspect_guest_card.py            Diagnostic: query local state
  repair_misattributed_notes.py    One-time repair for legacy note data
  requirements.txt                 Python dependencies
  .env.example                     Configuration template
  app/
    config.py                      Environment configuration loader
    appfolio_client.py             AppFolio Reports API client
    ghl_client.py                  GoHighLevel API client
    state_store.py                 SQLite state database layer
    mapper.py                      Guest card -> GHL contact/note mapping
    vacancy_mapper.py              Vacancy row -> GHL custom object mapping
    sync.py                        Guest card sync orchestration
    vacancy_sync.py                Vacancy sync orchestration
    vacancy_sync_main.py           Entry point: vacancy sync
    resync.py                      Refresh existing contacts and notes
    manage_round_robin.py          Interactive round-robin roster manager
    test_run.py                    Read-only connectivity smoke test
  scripts/
    fetch_sample_guest_card.py     Print a raw AppFolio guest card record
```

---

## Script Reference

### Entry points (scheduled)

#### `main.py`
Runs one guest card sync cycle: loads configuration, calls `app.sync.run_sync`, and exits `0` on success or `1` on any failure. Intended to be invoked on a schedule by a systemd timer. The working directory must be the project root so that `.env` and the `app` package resolve correctly.

#### `app/vacancy_sync_main.py`
Runs one vacancy sync cycle by calling `app.vacancy_sync.run_vacancy_sync`. It is standalone from the guest card sync and should be scheduled separately. It adds `/opt/appfolio-ghl-sync` to `sys.path`, so the application is expected to be installed at that path.

### Core modules (`app/`)

#### `app/config.py`
Loads `.env` (via python-dotenv) and environment variables into an immutable `Config` object. Fails fast with a clear message listing any missing required variables. Also builds the AppFolio Reports API base URL from `APPFOLIO_DATABASE`.

#### `app/appfolio_client.py`
HTTP client for the AppFolio Reports API.
- `fetch_guest_cards(from, to)` posts the guest card inquiries report request with a received-date range and follows `next_page_url` pagination until exhausted.
- `fetch_vacancies()` posts the unit vacancy report request, paginates, and applies the vacancy qualification rules described above.
- Uses HTTP basic authentication and automatic retries (3 attempts with backoff on 429 and 5xx responses).

#### `app/ghl_client.py`
HTTP client for the GoHighLevel (LeadConnector) API. Covers contacts (create, get, update, search), notes (list, create, update), users, calendar events (for out-of-office detection), pipelines, opportunities, and custom object schema and records. Errors are raised as `GHLApiError`, which carries the HTTP status code and response body so callers can react to specific conditions (for example, duplicate-contact and missing-record responses). Uses automatic retries on 429 and 5xx responses.

#### `app/state_store.py`
SQLite persistence layer. Tracks the sync watermark, contact identity mappings, per-inquiry sync records, per-unit vacancy records, and the round-robin roster and settings. See [Local State Database](#local-state-database).

#### `app/mapper.py`
Translates AppFolio guest card rows into GHL payloads. Handles AppFolio's "Last, First" name format, builds the contact payload and the inquiry note body, derives the per-inquiry key (`guest_card_id + received timestamp + property + unit`), generates the note dedup marker, normalizes user names for matching, and produces the email/phone secondary dedup key. Contacts are tagged `appfolio-guest-card`.

#### `app/vacancy_mapper.py`
Translates AppFolio vacancy rows into GHL custom object payloads. Converts values to the types GHL expects (integers, ISO dates, monetary objects) and maps AppFolio fields to the property object's field keys (for example `advertised_rent` to `rent_price`, `sqft` to `lot_size`).

#### `app/sync.py`
Orchestrates the guest card sync: contact resolution, agent assignment (round robin and out-of-office handling), opportunity creation, note creation with duplicate protection, and per-inquiry state recording. Also exposes `resync_inquiry`, used by `app/resync.py`.

#### `app/vacancy_sync.py`
Orchestrates the vacancy sync: validates that the GHL custom object exists, upserts a record per qualifying unit, recreates records that were deleted in GHL, and marks units that no longer qualify as "Unavailable".

### Operator tools

#### `app/test_run.py`
Read-only smoke test. Verifies that configuration loads, the AppFolio guest card and vacancy reports are reachable, GHL authentication works, and the GHL custom object schema exists. Creates and modifies nothing. Run it after deploying to a new environment or changing credentials.

```
.venv/bin/python app/test_run.py
```

#### `app/manage_round_robin.py`
Interactive menu for managing the round-robin roster: add or remove agents (with validation against live GHL user names), pause or resume an agent without losing their position, reorder the rotation, and enable or disable round robin. Changes are stored in the state database and take effect on the next sync run.

```
.venv/bin/python app/manage_round_robin.py
```

#### `app/resync.py`
Refreshes existing GHL data from AppFolio over a chosen date range. For each inquiry it updates the contact (fields and AppFolio-assigned agent) and updates the matching inquiry note in place, or creates the note if it is missing. It never creates contacts; if no existing contact can be found (by local state, then by email or phone search), the inquiry is reported as `missing_contact` and left alone. Runs as a dry run unless `--apply` is passed.

```
.venv/bin/python -m app.resync                      # prompts for number of days, dry run
.venv/bin/python -m app.resync --days 90            # dry run
.venv/bin/python -m app.resync --days 90 --apply    # apply changes
.venv/bin/python -m app.resync --from 2026-01-01 --apply
```

#### `full_resync.py`
Backfill tool that re-runs the standard per-inquiry sync over a date range wider than the normal five-day window, for recovering from outages or bulk contact deletion in GHL. It does not modify the watermark. Dry run by default. See the maintenance note regarding its current state.

```
.venv/bin/python full_resync.py --days 90
.venv/bin/python full_resync.py --days 90 --apply
.venv/bin/python full_resync.py --from 2026-01-01 --apply
```

#### `reset_state.py`
Clears the local "already synced" records (`contact_keys`, `synced_inquiries`, `synced_guest_cards`). Use it after contacts have been deleted in GHL; otherwise the sync will consider those inquiries complete and skip them. The state database is a cache of what already exists in GHL and does not check remote existence. Dry run by default; `--reset-watermark` also clears the stored watermark.

```
.venv/bin/python reset_state.py /opt/appfolio-ghl-sync/data/state.db
.venv/bin/python reset_state.py /opt/appfolio-ghl-sync/data/state.db --apply
```

#### `inspect_guest_card.py`
Read-only diagnostic for investigating why a specific guest card did not sync. Looks up a guest card ID, email, or phone number in the state database and prints matching rows.

```
.venv/bin/python inspect_guest_card.py /opt/appfolio-ghl-sync/data/state.db --guest-card-id 123456
.venv/bin/python inspect_guest_card.py /opt/appfolio-ghl-sync/data/state.db --email jane@example.com
```

#### `repair_misattributed_notes.py`
One-time migration utility. An earlier version of the duplicate-note check could attribute one GHL note to several guest cards. This script finds those rows in the legacy `synced_guest_cards` table and resets their note ID to `NULL` so a later run recreates the correct note. Dry run by default.

```
.venv/bin/python repair_misattributed_notes.py /opt/appfolio-ghl-sync/data/state.db
.venv/bin/python repair_misattributed_notes.py /opt/appfolio-ghl-sync/data/state.db --apply
```

#### `scripts/fetch_sample_guest_card.py`
Development helper. Fetches and prints one raw guest card record from AppFolio (default lookback 30 days, or pass a number of days as the first argument) so field names can be confirmed against `app/mapper.py`.

```
.venv/bin/python scripts/fetch_sample_guest_card.py 90
```

---

## Configuration

Configuration is read from a `.env` file in the project root or from the process environment. Copy `.env.example` to `.env` and populate it.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `APPFOLIO_CLIENT_ID` | Yes | | AppFolio Reports API client ID |
| `APPFOLIO_CLIENT_SECRET` | Yes | | AppFolio Reports API client secret |
| `APPFOLIO_DATABASE` | Yes | | AppFolio subdomain only (for example `mycompany` for `mycompany.appfolio.com`) |
| `GHL_PRIVATE_TOKEN` | Yes | | GoHighLevel Private Integration Token |
| `GHL_LOCATION_ID` | Yes | | GHL sub-account (location) ID |
| `APPFOLIO_GUEST_CARD_REPORT` | No | `guest_card_inquiries` | AppFolio guest card report resource name |
| `APPFOLIO_VACANCY_REPORT` | No | `unit_vacancy` | AppFolio vacancy report resource name |
| `GHL_PROPERTY_OBJECT_KEY` | No | `custom_objects.tmpl_properties` | GHL custom object key for property records (used by the vacancy sync) |
| `GHL_LEASING_PIPELINE_NAME` | No | `Leasing Pipeline` | Pipeline in which opportunities are created; the first stage is used |
| `GHL_OOO_CALENDAR_ID` | No | empty | GHL calendar used for out-of-office detection; check is skipped if unset |
| `STATE_DB_PATH` | No | `./data/state.db` | SQLite state database location |
| `INITIAL_LOOKBACK_DAYS` | No | `1` | Parsed by the configuration loader but not currently used by the sync |

Notes:
- `.env.example` lists only a subset of these variables. The optional variables above can be added to `.env` as needed.
- `STATE_DB_PATH` defaults to a path relative to the working directory. On the server, set it to an absolute path (for example `/opt/appfolio-ghl-sync/data/state.db`) so that scheduled jobs and manual runs always use the same database.
- The GHL Private Integration Token needs access to contacts, notes, users, calendar events (if out-of-office detection is used), opportunities and pipelines, and custom objects.

---

## Deployment on Debian 13

Debian 13 ships Python 3.13, which is the version the project was developed against. Several scripts expect the application to live at `/opt/appfolio-ghl-sync`.

### 1. System packages

```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 2. Service account and install directory

```
sudo useradd --system --home /opt/appfolio-ghl-sync --shell /usr/sbin/nologin appfolio
sudo mkdir -p /opt/appfolio-ghl-sync
sudo chown appfolio:appfolio /opt/appfolio-ghl-sync
```

### 3. Application and dependencies

Place the project in `/opt/appfolio-ghl-sync` (clone or copy it), then:

```
cd /opt/appfolio-ghl-sync
sudo -u appfolio python3 -m venv .venv
sudo -u appfolio .venv/bin/pip install -r requirements.txt
```

Dependencies are `requests` and `python-dotenv`.

### 4. Configuration

```
sudo -u appfolio cp .env.example .env
sudo -u appfolio nano .env
sudo chmod 600 /opt/appfolio-ghl-sync/.env
sudo -u appfolio mkdir -p /opt/appfolio-ghl-sync/data
```

The `.env` file contains credentials and must not be committed to version control. The repository `.gitignore` already excludes `.env`, `*.db`, `.venv/`, and `data/`.

### 5. Verify

```
cd /opt/appfolio-ghl-sync
sudo -u appfolio .venv/bin/python app/test_run.py
```

All checks should report `[OK]`.

### 6. Configure the round-robin roster (optional)

```
sudo -u appfolio .venv/bin/python app/manage_round_robin.py
```

---

## Scheduling with systemd

The following unit files are provided as a starting point. Adjust the intervals to match business needs and AppFolio API usage limits.

**`/etc/systemd/system/appfolio-ghl-sync.service`**
```ini
[Unit]
Description=AppFolio to GoHighLevel guest card sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=appfolio
WorkingDirectory=/opt/appfolio-ghl-sync
ExecStart=/opt/appfolio-ghl-sync/.venv/bin/python main.py
```

**`/etc/systemd/system/appfolio-ghl-sync.timer`**
```ini
[Unit]
Description=Run the guest card sync periodically

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
```

**`/etc/systemd/system/appfolio-ghl-vacancy-sync.service`**
```ini
[Unit]
Description=AppFolio to GoHighLevel vacancy sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=appfolio
WorkingDirectory=/opt/appfolio-ghl-sync
ExecStart=/opt/appfolio-ghl-sync/.venv/bin/python app/vacancy_sync_main.py
```

**`/etc/systemd/system/appfolio-ghl-vacancy-sync.timer`**
```ini
[Unit]
Description=Run the vacancy sync periodically

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timers:

```
sudo systemctl daemon-reload
sudo systemctl enable --now appfolio-ghl-sync.timer
sudo systemctl enable --now appfolio-ghl-vacancy-sync.timer
systemctl list-timers 'appfolio-*'
```

---

## Operations and Maintenance

### Viewing logs

Both entry points log to standard output and standard error, which systemd captures in the journal.

```
journalctl -u appfolio-ghl-sync.service -n 100 --no-pager
journalctl -u appfolio-ghl-vacancy-sync.service -n 100 --no-pager
journalctl -u appfolio-ghl-sync.service -f
```

A guest card run logs a summary line of counts (new, grouped, repaired, duplicate, already synced). Exit status `1` indicates configuration or sync errors; check the journal for the underlying exception.

### Common procedures

| Situation | Procedure |
|---|---|
| Credentials changed or new environment | Run `app/test_run.py` |
| Add, remove, pause, or reorder agents | Run `app/manage_round_robin.py` |
| Contacts were deleted in GHL and need to be recreated | Run `reset_state.py` (dry run, then `--apply`), then backfill |
| A specific lead did not sync | Run `inspect_guest_card.py` against the state database; check the journal for that date |
| Existing contacts or notes need to be refreshed with current AppFolio data | Run `app.resync` (dry run, then `--apply`) |
| Gap longer than the five-day window | Backfill with `full_resync.py` after confirming the maintenance note below |

### Backups

The state database is a cache of synced records and can be rebuilt, but it also holds the round-robin roster and rotation position, which are not stored anywhere else. Include `data/state.db` in routine backups.

---

## Local State Database

SQLite file at `STATE_DB_PATH`. Schema is created automatically on first use.

| Table | Purpose |
|---|---|
| `contact_keys` | Maps `gc:<guest_card_id>`, `email:<address>`, and `phone:<digits>` keys to GHL contact IDs |
| `synced_inquiries` | Per-inquiry record of the GHL contact and note created; the idempotency record for guest card sync |
| `synced_vacancies` | Maps AppFolio `unit_id` to the GHL property record ID |
| `round_robin_agents` | Agent roster with rotation order and active flag |
| `round_robin_settings` | Round-robin enabled flag and last assigned agent |
| `sync_state` | Sync watermark (`last_received_on_to`) |
| `synced_guest_cards` | Legacy table from an earlier design; retained for compatibility only |

---
