# Ticket Desk

Small Flask app for managing support tickets with local SQLite tracking and a replaceable USU SM service layer.

By default the app uses a mock USU SM service so ticket work can continue when the real API is not available. Set `USM_SERVICE_MODE=auto` or `USM_SERVICE_MODE=api` when you are ready to connect it to USU SM.

## Ticket Fields

Each ticket stores:

- ID
- Title
- Description
- Category
- Priority: `Low`, `Medium`, `High`, `Critical`
- Status: `Open`, `In Progress`, `Resolved`, `Closed`
- Created by
- Assigned to
- Created/updated/closed timestamps
- Comments / notes
- Change history

Tickets are saved in a local SQLite database at `instance/tickets.sqlite3` by default. Existing databases are migrated on startup.

## Status Rules

Status changes must follow this flow:

```text
Open -> In Progress -> Resolved -> Closed
```

Tickets can be reopened from `Resolved` or `Closed` to `Open`, but a reason is required.

## Setup

### Recommended: VS Code / npm wrapper

This is a Flask/Python app, but it includes a small `package.json` wrapper so you can use the requested npm workflow.

1. Install prerequisites:

   - Python 3.10+
   - Node.js / npm

2. Install local dependencies:

   ```bash
   npm install
   ```

   This creates `.venv` and installs `requirements.txt`.

3. Run the app:

   ```bash
   npm start
   ```

4. Open the app:

   ```text
   http://127.0.0.1:5000
   ```

If you need access from another browser/network namespace, run:

```bash
npm run start:lan
```

### Direct Python setup

1. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:

   ```bash
   flask --app app run --debug
   ```

4. Open the app:

   ```text
   http://127.0.0.1:5000
   ```

## Local Checks

After `npm install`, run:

```bash
npm run check
```

## API Endpoints

- `GET /tickets`
- `GET /tickets/:id`
- `GET /tickets/:id/status`
- `POST /tickets`
- `PUT /tickets/:id`
- `DELETE /tickets/:id`
- `PATCH /tickets/:id/status`
- `POST /tickets/:id/comments`
- `GET /tickets/:id/history`

For JSON responses, send `Accept: application/json` or add `?format=json`.

## Optional Configuration

Create a `.env` file to configure local settings:

```env
FLASK_SECRET_KEY=change-this-for-shared-use
TICKET_DATABASE=instance/tickets.sqlite3

USM_SERVICE_MODE=mock
USM_EXECWF_URL=http://localhost:8087/vmwebjetty/services/api/execwf
USM_ACCESS_TOKEN=replace-with-access-token
USM_SERVICE=InterfaceTransactionStart
USM_USERNAME=replace-with-username
USM_PASSWORD=replace-with-password
USM_ENCRYPTED=N
USM_CLIENT=01
USM_INTERFACE_ACTION=VMEx_VM_CreateTicket_complex
USM_IMPACT="3 Medium"
USM_URGENCY="3 Medium"
USM_REQUESTED_BY_EMAIL=norbert.nutzer@usu.com
USM_AFFECTED_EMAIL=norbert.nutzer@usu.com
USM_STATUS=IN_CRE
USM_TIMEOUT_SECONDS=30
```

The app creates the database automatically on startup.

`USM_SERVICE_MODE` options:

- `mock`: use the local mock adapter only.
- `auto`: try the API when credentials exist, then fall back to mock on unavailable or unimplemented operations.
- `api`: use the real API adapter and surface API errors.

## USM Field Mapping

The create form is mapped to the USU SM request like this:

- `Ticket title` -> `params.fields.summary`
- `Details` -> `params.fields.description`
- Impact, urgency, and priority are set from the ticket priority and fall back to `.env` only if the priority is missing or unknown
- The USM `priority` input is sent with the same value as urgency
- Critical priority maps to impact `1 Severe`, urgency `1 Critical`, and priority `1 Critical`
- Status, client, username, password, and token are read from `.env`
- `persEmailReqBy` and `persEmailAffected` are sent because USM expects them, but they are not shown in the app form; if omitted from `.env`, both default to `norbert.nutzer@usu.com`

Other ticket operations already go through `usu_sm_service.py`; their current API implementations intentionally fall back to mock in `auto` mode until the exact USU SM endpoints/contracts are available.

## WSL and Windows Localhost

If this app runs in Ubuntu/WSL and USM runs on Windows at `http://localhost:8087`, Ubuntu may not be able to reach that Windows-only loopback address directly.

The app handles this local setup by trying the normal Python POST first. If that fails for a `localhost` URL inside WSL, it retries the same POST through Windows PowerShell, where Windows `localhost:8087` is reachable.
