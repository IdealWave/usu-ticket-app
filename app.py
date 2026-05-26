import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for

from usu_sm_service import ServiceResult, USUSMServiceError, get_usu_sm_service


load_dotenv(Path(__file__).with_name(".env"), override=True)

app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "local-ticket-app")
app.config["DATABASE"] = os.getenv(
    "TICKET_DATABASE",
    str(Path(app.instance_path) / "tickets.sqlite3"),
)


PRIORITIES = ("Low", "Medium", "High", "Critical")
STATUSES = ("Open", "In Progress", "Resolved", "Closed")
STATUS_TRANSITIONS = {
    "Open": ("In Progress",),
    "In Progress": ("Resolved",),
    "Resolved": ("Closed", "Open"),
    "Closed": ("Open",),
}
REOPEN_STATUSES = {"Resolved", "Closed"}

REQUIRED_FIELDS = {
    "title": "Ticket title",
    "description": "Details",
    "category": "Category",
    "priority": "Priority",
    "created_by": "Created by",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    priority TEXT NOT NULL DEFAULT 'Medium',
    status TEXT NOT NULL DEFAULT 'Open',
    created_by TEXT NOT NULL DEFAULT '',
    assigned_to TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
"""

MISSING_TICKET_COLUMNS = {
    "first_name": "first_name TEXT NOT NULL DEFAULT ''",
    "last_name": "last_name TEXT NOT NULL DEFAULT ''",
    "category": "category TEXT NOT NULL DEFAULT 'General'",
    "priority": "priority TEXT NOT NULL DEFAULT 'Medium'",
    "created_by": "created_by TEXT NOT NULL DEFAULT ''",
    "assigned_to": "assigned_to TEXT NOT NULL DEFAULT ''",
}


class TicketValidationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_database() -> None:
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        migrate_tickets_table(db)
        normalize_ticket_data(db)
        seed_existing_ticket_history(db)
        db.commit()


def migrate_tickets_table(db: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(tickets)").fetchall()
    }

    for column, definition in MISSING_TICKET_COLUMNS.items():
        if column not in columns:
            db.execute(f"ALTER TABLE tickets ADD COLUMN {definition}")


def normalize_ticket_data(db: sqlite3.Connection) -> None:
    db.execute("UPDATE tickets SET status = 'Open' WHERE lower(status) = 'open'")
    db.execute("UPDATE tickets SET status = 'Closed' WHERE lower(status) = 'closed'")
    db.execute(
        """
        UPDATE tickets
        SET status = 'Open'
        WHERE status NOT IN ('Open', 'In Progress', 'Resolved', 'Closed')
        """
    )
    db.execute(
        """
        UPDATE tickets
        SET priority = 'Medium'
        WHERE priority IS NULL
           OR priority = ''
           OR priority NOT IN ('Low', 'Medium', 'High', 'Critical')
        """
    )
    db.execute(
        """
        UPDATE tickets
        SET category = 'General'
        WHERE category IS NULL OR trim(category) = ''
        """
    )
    db.execute(
        """
        UPDATE tickets
        SET created_by = trim(first_name || ' ' || last_name)
        WHERE (created_by IS NULL OR trim(created_by) = '')
          AND (trim(first_name) != '' OR trim(last_name) != '')
        """
    )
    db.execute(
        """
        UPDATE tickets
        SET created_by = 'Unknown'
        WHERE created_by IS NULL OR trim(created_by) = ''
        """
    )
    db.execute(
        """
        UPDATE tickets
        SET assigned_to = ''
        WHERE assigned_to IS NULL
        """
    )


def seed_existing_ticket_history(db: sqlite3.Connection) -> None:
    now = utc_now()
    rows = db.execute(
        """
        SELECT id, created_by
        FROM tickets AS ticket
        WHERE NOT EXISTS (
            SELECT 1
            FROM ticket_history AS history
            WHERE history.ticket_id = ticket.id
        )
        """
    ).fetchall()

    for row in rows:
        db.execute(
            """
            INSERT INTO ticket_history (
                ticket_id, actor, action, field, old_value, new_value, note, created_at
            )
            VALUES (?, ?, 'migrated', NULL, NULL, NULL, ?, ?)
            """,
            (
                row["id"],
                row["created_by"] or "System",
                "Existing ticket registered in expanded ticket module.",
                now,
            ),
        )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.template_filter("format_datetime")
def format_datetime(value: str | None) -> str:
    if not value:
        return ""

    parsed = datetime.fromisoformat(value)
    return parsed.astimezone().strftime("%b %d, %Y %H:%M")


@app.template_filter("status_class")
def status_class(value: str | None) -> str:
    return (value or "").lower().replace(" ", "-")


@app.template_filter("priority_class")
def priority_class(value: str | None) -> str:
    return (value or "").lower()


def wants_json() -> bool:
    if request.args.get("format") == "json":
        return True
    if request.is_json:
        return True
    if request.method in {"PUT", "PATCH", "DELETE"}:
        return True

    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return (
        best == "application/json"
        and request.accept_mimetypes[best] >= request.accept_mimetypes["text/html"]
    )


def request_data():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def request_value(data, *names: str, default: str = "") -> str:
    for name in names:
        value = data.get(name)
        if value is not None:
            return str(value).strip()
    return default


def bool_value(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "confirm"}


def normalize_choice(value: str, choices: tuple[str, ...]) -> str:
    value = str(value or "").strip()
    for choice in choices:
        if value.lower() == choice.lower():
            return choice
    return value


def split_created_by(created_by: str) -> tuple[str, str]:
    parts = created_by.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def request_actor(default: str = "System") -> str:
    data = request_data()
    actor = request_value(data, "actor", "updated_by", "updatedBy", "author", default=default)
    return actor or default


def ticket_from_request(existing: dict | None = None) -> dict:
    data = request_data()
    fallback = existing or {}
    first_name = request_value(data, "first_name", default="")
    last_name = request_value(data, "last_name", default="")
    created_by = request_value(data, "created_by", "createdBy", default="")

    if not created_by and (first_name or last_name):
        created_by = " ".join(part for part in (first_name, last_name) if part)

    priority = normalize_choice(
        request_value(data, "priority", default=fallback.get("priority", "")),
        PRIORITIES,
    )

    return {
        "title": request_value(data, "title", default=fallback.get("title", "")),
        "description": request_value(
            data,
            "description",
            "details",
            default=fallback.get("description", ""),
        ),
        "category": request_value(
            data,
            "category",
            default=fallback.get("category", ""),
        ),
        "priority": priority,
        "created_by": created_by or fallback.get("created_by", ""),
        "assigned_to": request_value(
            data,
            "assigned_to",
            "assignedTo",
            default=fallback.get("assigned_to", ""),
        ),
    }


def validate_ticket(ticket: dict) -> list[str]:
    errors = []
    for field, label in REQUIRED_FIELDS.items():
        if not ticket.get(field):
            errors.append(f"{label} is required.")

    if ticket.get("priority") and ticket["priority"] not in PRIORITIES:
        errors.append(f"Priority must be one of: {', '.join(PRIORITIES)}.")

    return errors


def current_filters() -> dict:
    status = normalize_choice(request.args.get("status", ""), STATUSES)
    priority = normalize_choice(request.args.get("priority", ""), PRIORITIES)
    return {
        "search": request.args.get("search", "").strip(),
        "status": status if status in STATUSES else "",
        "priority": priority if priority in PRIORITIES else "",
        "date": request.args.get("date", "").strip(),
    }


def all_tickets(filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    clauses = []
    params: list[str | int] = []

    if filters.get("status"):
        clauses.append("status = ?")
        params.append(filters["status"])

    if filters.get("priority"):
        clauses.append("priority = ?")
        params.append(filters["priority"])

    if filters.get("date"):
        clauses.append("substr(created_at, 1, 10) = ?")
        params.append(filters["date"])

    if filters.get("search"):
        search = filters["search"]
        if search.isdigit():
            clauses.append("(id = ? OR title LIKE ?)")
            params.extend([int(search), f"%{search}%"])
        else:
            clauses.append("title LIKE ?")
            params.append(f"%{search}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().execute(
        f"""
        SELECT *
        FROM tickets
        {where_sql}
        ORDER BY
            CASE status
                WHEN 'Open' THEN 0
                WHEN 'In Progress' THEN 1
                WHEN 'Resolved' THEN 2
                ELSE 3
            END,
            updated_at DESC
        """,
        params,
    ).fetchall()

    return [dict(row) for row in rows]


def get_ticket(ticket_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()

    if row is None:
        return None

    return dict(row)


def comments_for_ticket(ticket_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT *
        FROM ticket_comments
        WHERE ticket_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (ticket_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def history_for_ticket(ticket_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT *
        FROM ticket_history
        WHERE ticket_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (ticket_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def detail_context(ticket: dict | None) -> dict | None:
    if ticket is None:
        return None

    enriched = dict(ticket)
    enriched["comments"] = comments_for_ticket(ticket["id"])
    enriched["history"] = history_for_ticket(ticket["id"])
    enriched["allowed_statuses"] = STATUS_TRANSITIONS.get(ticket["status"], ())
    return enriched


def render_index(
    edit_ticket: dict | None = None,
    form_data: dict | None = None,
    detail_ticket: dict | None = None,
):
    filters = current_filters()
    return render_template(
        "index.html",
        tickets=all_tickets(filters),
        edit_ticket=edit_ticket,
        form_data=form_data or {},
        detail_ticket=detail_context(detail_ticket),
        filters=filters,
        priorities=PRIORITIES,
        statuses=STATUSES,
    )


def api_error(message: str, status_code: int = 400, errors: list[str] | None = None):
    payload = {"error": message}
    if errors:
        payload["errors"] = errors
    return jsonify(payload), status_code


def ticket_json(ticket: dict, include_related: bool = False) -> dict:
    payload = {
        "id": ticket["id"],
        "title": ticket["title"],
        "description": ticket["description"],
        "category": ticket["category"],
        "priority": ticket["priority"],
        "status": ticket["status"],
        "createdBy": ticket["created_by"],
        "assignedTo": ticket["assigned_to"],
        "createdAt": ticket["created_at"],
        "updatedAt": ticket["updated_at"],
        "closedAt": ticket["closed_at"],
    }

    if include_related:
        payload["comments"] = [comment_json(comment) for comment in comments_for_ticket(ticket["id"])]
        payload["history"] = [history_json(item) for item in history_for_ticket(ticket["id"])]

    return payload


def comment_json(comment: dict) -> dict:
    return {
        "id": comment["id"],
        "ticketId": comment["ticket_id"],
        "author": comment["author"],
        "body": comment["body"],
        "createdAt": comment["created_at"],
    }


def history_json(item: dict) -> dict:
    return {
        "id": item["id"],
        "ticketId": item["ticket_id"],
        "actor": item["actor"],
        "action": item["action"],
        "field": item["field"],
        "oldValue": item["old_value"],
        "newValue": item["new_value"],
        "note": item["note"],
        "createdAt": item["created_at"],
    }


def log_history(
    ticket_id: int,
    actor: str,
    action: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    note: str | None = None,
) -> None:
    get_db().execute(
        """
        INSERT INTO ticket_history (
            ticket_id, actor, action, field, old_value, new_value, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, actor or "System", action, field, old_value, new_value, note, utc_now()),
    )


def log_service_result(ticket_id: int, actor: str, result: ServiceResult) -> None:
    note = result.message
    if result.external_id:
        note = f"{note} External reference: {result.external_id}."

    log_history(
        ticket_id,
        actor,
        "usu_sm_sync",
        result.action,
        None,
        result.status,
        note,
    )


def service_flash_message(result: ServiceResult, success_text: str) -> str:
    if result.provider == "mock":
        return f"{success_text} Mock USU SM service was used."
    return success_text


def insert_ticket(ticket: dict) -> int:
    now = utc_now()
    first_name, last_name = split_created_by(ticket["created_by"])
    cursor = get_db().execute(
        """
        INSERT INTO tickets (
            first_name,
            last_name,
            title,
            description,
            category,
            priority,
            status,
            created_by,
            assigned_to,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?)
        """,
        (
            first_name,
            last_name,
            ticket["title"],
            ticket["description"],
            ticket["category"],
            ticket["priority"],
            ticket["created_by"],
            ticket["assigned_to"],
            now,
            now,
        ),
    )
    ticket_id = cursor.lastrowid
    log_history(
        ticket_id,
        ticket["created_by"],
        "created",
        "status",
        None,
        "Open",
        "Ticket created.",
    )
    get_db().commit()
    return ticket_id


def changed_ticket_fields(existing: dict, updated: dict) -> list[tuple[str, str, str]]:
    fields = ("title", "description", "category", "priority", "created_by", "assigned_to")
    changes = []
    for field in fields:
        old_value = existing.get(field) or ""
        new_value = updated.get(field) or ""
        if old_value != new_value:
            changes.append((field, old_value, new_value))
    return changes


def update_ticket_record(
    ticket_id: int,
    updated: dict,
    actor: str,
) -> tuple[list[tuple[str, str, str]], ServiceResult | None]:
    existing = get_ticket(ticket_id)
    if existing is None:
        raise TicketValidationError(f"Ticket #{ticket_id} was not found.", 404)

    errors = validate_ticket(updated)
    if errors:
        raise TicketValidationError("Please fix the ticket fields: " + " ".join(errors))

    changes = changed_ticket_fields(existing, updated)
    if not changes:
        return [], None

    first_name, last_name = split_created_by(updated["created_by"])
    get_db().execute(
        """
        UPDATE tickets
        SET first_name = ?,
            last_name = ?,
            title = ?,
            description = ?,
            category = ?,
            priority = ?,
            created_by = ?,
            assigned_to = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            first_name,
            last_name,
            updated["title"],
            updated["description"],
            updated["category"],
            updated["priority"],
            updated["created_by"],
            updated["assigned_to"],
            utc_now(),
            ticket_id,
        ),
    )

    for field, old_value, new_value in changes:
        log_history(ticket_id, actor, "updated", field, old_value, new_value)

    service_payload = dict(updated)
    service_payload["id"] = ticket_id
    service_payload["status"] = existing["status"]
    service_result = get_usu_sm_service().update_ticket(service_payload, changes)
    log_service_result(ticket_id, actor, service_result)
    get_db().commit()
    return changes, service_result


def change_ticket_status(
    ticket_id: int,
    new_status: str,
    actor: str,
    reason: str = "",
) -> tuple[dict, ServiceResult]:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise TicketValidationError(f"Ticket #{ticket_id} was not found.", 404)

    new_status = normalize_choice(new_status, STATUSES)
    if new_status not in STATUSES:
        raise TicketValidationError(f"Status must be one of: {', '.join(STATUSES)}.")

    old_status = ticket["status"]
    if old_status == new_status:
        raise TicketValidationError(f"Ticket #{ticket_id} is already {new_status}.")

    if new_status not in STATUS_TRANSITIONS.get(old_status, ()):
        allowed = ", ".join(STATUS_TRANSITIONS.get(old_status, ())) or "none"
        raise TicketValidationError(
            f"Invalid status transition from {old_status} to {new_status}. Allowed: {allowed}."
        )

    is_reopen = old_status in REOPEN_STATUSES and new_status == "Open"
    if is_reopen and not reason:
        raise TicketValidationError("A reason is required when reopening a ticket.")

    now = utc_now()
    closed_at = ticket["closed_at"]
    if new_status == "Closed":
        closed_at = now
    elif new_status == "Open":
        closed_at = None

    get_db().execute(
        """
        UPDATE tickets
        SET status = ?,
            updated_at = ?,
            closed_at = ?
        WHERE id = ?
        """,
        (new_status, now, closed_at, ticket_id),
    )

    action = "reopened" if is_reopen else "closed" if new_status == "Closed" else "status_changed"
    log_history(ticket_id, actor, action, "status", old_status, new_status, reason or None)
    service_result = get_usu_sm_service().change_status(ticket, new_status, actor, reason)
    log_service_result(ticket_id, actor, service_result)
    get_db().commit()
    return get_ticket(ticket_id), service_result


def add_ticket_comment(ticket_id: int, author: str, body: str) -> tuple[dict, ServiceResult]:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise TicketValidationError(f"Ticket #{ticket_id} was not found.", 404)

    if not author:
        raise TicketValidationError("Comment author is required.")
    if not body:
        raise TicketValidationError("Comment text is required.")

    cursor = get_db().execute(
        """
        INSERT INTO ticket_comments (ticket_id, author, body, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (ticket_id, author, body, utc_now()),
    )
    log_history(ticket_id, author, "commented", "comments", None, body[:160])
    service_result = get_usu_sm_service().add_comment(ticket, author, body)
    log_service_result(ticket_id, author, service_result)
    get_db().commit()

    row = get_db().execute(
        "SELECT * FROM ticket_comments WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row), service_result


def delete_ticket_record(ticket_id: int, actor: str, confirmed: bool) -> ServiceResult:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise TicketValidationError(f"Ticket #{ticket_id} was not found.", 404)

    if not confirmed:
        raise TicketValidationError("Ticket deletion requires confirmation.")

    log_history(
        ticket_id,
        actor,
        "deleted",
        "ticket",
        ticket["title"],
        None,
        "Ticket deleted.",
    )
    service_result = get_usu_sm_service().delete_ticket(ticket, actor)
    log_service_result(ticket_id, actor, service_result)
    get_db().execute("DELETE FROM ticket_comments WHERE ticket_id = ?", (ticket_id,))
    get_db().execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    get_db().commit()
    return service_result


@app.get("/")
def index():
    return render_index()


@app.get("/tickets")
def tickets_view():
    if wants_json():
        return jsonify({"tickets": [ticket_json(ticket) for ticket in all_tickets(current_filters())]})

    return render_index()


@app.get("/tickets/<int:ticket_id>")
def ticket_detail(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        if wants_json():
            return api_error(f"Ticket #{ticket_id} was not found.", 404)
        abort(404)

    if wants_json():
        return jsonify(ticket_json(ticket, include_related=True))

    return render_index(detail_ticket=ticket)


@app.get("/tickets/<int:ticket_id>/status")
def ticket_status(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return api_error(f"Ticket #{ticket_id} was not found.", 404)

    try:
        service_result = get_usu_sm_service().read_ticket_status(ticket)
    except USUSMServiceError as exc:
        return api_error(f"USU SM status read failed: {exc}", 502)

    return jsonify(
        {
            "ticketId": ticket_id,
            "status": ticket["status"],
            "closedAt": ticket["closed_at"],
            "updatedAt": ticket["updated_at"],
            "usuSm": service_result.to_dict(),
        }
    )


@app.post("/tickets")
def create_ticket():
    ticket = ticket_from_request()
    errors = validate_ticket(ticket)

    if errors:
        if wants_json():
            return api_error("Ticket validation failed.", 400, errors)
        flash("Please fix the ticket fields: " + " ".join(errors), "error")
        return render_index(form_data=ticket), 400

    try:
        service_result = get_usu_sm_service().create_ticket(ticket)
    except USUSMServiceError as exc:
        if wants_json():
            return api_error(f"USU SM ticket creation failed: {exc}", 502)
        flash(f"USU SM ticket creation failed: {exc}", "error")
        return render_index(form_data=ticket), 502

    ticket_id = insert_ticket(ticket)
    log_service_result(ticket_id, ticket["created_by"], service_result)
    get_db().commit()
    created = get_ticket(ticket_id)

    if wants_json():
        payload = ticket_json(created, include_related=True)
        payload["usuSm"] = service_result.to_dict()
        return jsonify(payload), 201

    flash(service_flash_message(service_result, "Ticket created."), "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.get("/tickets/<int:ticket_id>/edit")
def edit_ticket(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        abort(404)

    return render_index(edit_ticket=ticket, detail_ticket=ticket)


@app.post("/tickets/<int:ticket_id>/edit")
def update_ticket_form(ticket_id: int):
    existing = get_ticket(ticket_id)
    if existing is None:
        abort(404)

    updated = ticket_from_request(existing)
    actor = request_actor(updated.get("created_by") or existing.get("created_by") or "System")

    try:
        changes, service_result = update_ticket_record(ticket_id, updated, actor)
    except TicketValidationError as exc:
        invalid_ticket = dict(existing)
        invalid_ticket.update(updated)
        flash(str(exc), "error")
        return render_index(edit_ticket=invalid_ticket, detail_ticket=existing), exc.status_code
    except USUSMServiceError as exc:
        invalid_ticket = dict(existing)
        invalid_ticket.update(updated)
        flash(f"USU SM update failed: {exc}", "error")
        return render_index(edit_ticket=invalid_ticket, detail_ticket=existing), 502

    if changes:
        flash(
            service_flash_message(service_result, f"Ticket #{ticket_id} updated."),
            "success",
        )
    else:
        flash(f"Ticket #{ticket_id} had no changes.", "info")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.put("/tickets/<int:ticket_id>")
def update_ticket_api(ticket_id: int):
    existing = get_ticket(ticket_id)
    if existing is None:
        return api_error(f"Ticket #{ticket_id} was not found.", 404)

    updated = ticket_from_request(existing)
    actor = request_actor(updated.get("created_by") or existing.get("created_by") or "System")

    try:
        changes, service_result = update_ticket_record(ticket_id, updated, actor)
    except TicketValidationError as exc:
        return api_error(str(exc), exc.status_code)
    except USUSMServiceError as exc:
        return api_error(f"USU SM update failed: {exc}", 502)

    payload = ticket_json(get_ticket(ticket_id), include_related=True)
    payload["changedFields"] = [field for field, _, _ in changes]
    payload["usuSm"] = service_result.to_dict() if service_result else None
    return jsonify(payload)


@app.delete("/tickets/<int:ticket_id>")
def delete_ticket_api(ticket_id: int):
    data = request_data()
    confirmed = bool_value(data.get("confirm") or request.args.get("confirm"))
    actor = request_actor()

    try:
        service_result = delete_ticket_record(ticket_id, actor, confirmed)
    except TicketValidationError as exc:
        return api_error(str(exc), exc.status_code)
    except USUSMServiceError as exc:
        return api_error(f"USU SM delete sync failed: {exc}", 502)

    return jsonify(
        {
            "message": f"Ticket #{ticket_id} deleted.",
            "usuSm": service_result.to_dict(),
        }
    )


@app.post("/tickets/<int:ticket_id>/delete")
def delete_ticket_form(ticket_id: int):
    data = request_data()
    confirmed = bool_value(data.get("confirm"))
    actor = request_actor()

    try:
        service_result = delete_ticket_record(ticket_id, actor, confirmed)
    except TicketValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    except USUSMServiceError as exc:
        flash(f"USU SM delete sync failed: {exc}", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    flash(service_flash_message(service_result, f"Ticket #{ticket_id} deleted."), "success")
    return redirect(url_for("tickets_view"))


@app.patch("/tickets/<int:ticket_id>/status")
def update_ticket_status_api(ticket_id: int):
    data = request_data()
    new_status = request_value(data, "status", "new_status", "newStatus")
    reason = request_value(data, "reason", "reopen_reason", "reopenReason")
    actor = request_actor()

    try:
        ticket, service_result = change_ticket_status(ticket_id, new_status, actor, reason)
    except TicketValidationError as exc:
        return api_error(str(exc), exc.status_code)
    except USUSMServiceError as exc:
        return api_error(f"USU SM status update failed: {exc}", 502)

    payload = ticket_json(ticket, include_related=True)
    payload["usuSm"] = service_result.to_dict()
    return jsonify(payload)


@app.post("/tickets/<int:ticket_id>/status")
def update_ticket_status_form(ticket_id: int):
    data = request_data()
    new_status = request_value(data, "status", "new_status", "newStatus")
    reason = request_value(data, "reason", "reopen_reason", "reopenReason")
    actor = request_actor()

    try:
        _, service_result = change_ticket_status(ticket_id, new_status, actor, reason)
    except TicketValidationError as exc:
        flash(str(exc), "error")
    except USUSMServiceError as exc:
        flash(f"USU SM status update failed: {exc}", "error")
    else:
        flash(
            service_flash_message(service_result, f"Ticket #{ticket_id} status changed to {new_status}."),
            "success",
        )

    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.post("/tickets/<int:ticket_id>/close")
def close_ticket(ticket_id: int):
    actor = request_actor()

    try:
        _, service_result = change_ticket_status(ticket_id, "Closed", actor)
    except TicketValidationError as exc:
        flash(str(exc), "error")
    except USUSMServiceError as exc:
        flash(f"USU SM close failed: {exc}", "error")
    else:
        flash(service_flash_message(service_result, f"Ticket #{ticket_id} closed."), "success")

    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.post("/tickets/<int:ticket_id>/comments")
def create_comment(ticket_id: int):
    data = request_data()
    author = request_value(data, "author", "actor")
    body = request_value(data, "body", "comment", "note")

    try:
        comment, service_result = add_ticket_comment(ticket_id, author, body)
    except TicketValidationError as exc:
        if wants_json():
            return api_error(str(exc), exc.status_code)
        flash(str(exc), "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    except USUSMServiceError as exc:
        if wants_json():
            return api_error(f"USU SM comment sync failed: {exc}", 502)
        flash(f"USU SM comment sync failed: {exc}", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if wants_json():
        payload = comment_json(comment)
        payload["usuSm"] = service_result.to_dict()
        return jsonify(payload), 201

    flash(service_flash_message(service_result, "Comment added."), "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.get("/tickets/<int:ticket_id>/history")
def ticket_history(ticket_id: int):
    ticket = get_ticket(ticket_id)
    history = history_for_ticket(ticket_id)
    if ticket is None and not history:
        return api_error(f"Ticket #{ticket_id} was not found.", 404)

    return jsonify(
        {
            "ticketId": ticket_id,
            "history": [history_json(item) for item in history],
        }
    )


init_database()


if __name__ == "__main__":
    app.run(debug=True)
