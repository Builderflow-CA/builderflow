import os
import json
import html
import sqlite3
from contextlib import contextmanager

try:
    import psycopg
    from psycopg.rows import dict_row
    PSYCOPG_AVAILABLE = True
except ImportError:
    psycopg = None
    dict_row = None
    PSYCOPG_AVAILABLE = False
from io import BytesIO
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Any, Iterable

import pandas as pd
import altair as alt
import streamlit as st
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import resend
    RESEND_AVAILABLE = True
except ImportError:
    resend = None
    RESEND_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


# ============================================================
# App Setup
# ============================================================

load_dotenv()

APP_NAME = "BuilderFlow"
DB_FILE = Path("builderflow.db")
LEGACY_JSON_FILE = Path("builderflow_data.json")
LOCAL_COMPANY_ID = 1

st.set_page_config(page_title=APP_NAME, page_icon="🏗️", layout="wide")

def read_secret_or_env(name: str, default: str = "") -> str:
    """Read a setting from environment variables first, then Streamlit secrets."""
    value = os.getenv(name)
    if value not in (None, ""):
        return str(value)
    try:
        secret_value = st.secrets.get(name, default)
        return str(secret_value) if secret_value not in (None, "") else default
    except Exception:
        return default

DATABASE_URL = read_secret_or_env("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
AUTH_REQUIRED = read_secret_or_env("BUILDERFLOW_AUTH_REQUIRED", "false").strip().lower() == "true"
ACTIVE_COMPANY_ID = LOCAL_COMPANY_ID
ACTIVE_OWNER_KEY = "local-demo"
ACTIVE_USER_LABEL = "Local Demo"


# ============================================================
# Core Utilities
# ============================================================

def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_string() -> str:
    return date.today().isoformat()


def future_date_string(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def parse_iso_date(value: str | None, default_today: bool = True) -> date:
    try:
        if not value:
            return date.today() if default_today else date(1970, 1, 1)
        return date.fromisoformat(str(value))
    except ValueError:
        return date.today() if default_today else date(1970, 1, 1)


def safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        value = str(value).replace("$", "").replace(",", "").strip()
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


def clean_pdf_text(text: Any) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def text_to_html(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br>")


def normalize_optional_text(value: Any) -> str:
    return "" if value is None else str(value)


# ============================================================
# Database Layer — Local SQLite or Cloud Postgres
# ============================================================

def postgres_query(query: str) -> str:
    """Translate simple SQLite-style placeholders into psycopg placeholders."""
    return query.replace("?", "%s")


@contextmanager
def db_connection():
    if USE_POSTGRES:
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "DATABASE_URL is configured, but psycopg is not installed. "
                "Run: pip install 'psycopg[binary]'"
            )
        connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
    else:
        connection = sqlite3.connect(DB_FILE, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def fetch_one(query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with db_connection() as con:
        if USE_POSTGRES:
            row = con.execute(postgres_query(query), tuple(params)).fetchone()
        else:
            row = con.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with db_connection() as con:
        if USE_POSTGRES:
            rows = con.execute(postgres_query(query), tuple(params)).fetchall()
        else:
            rows = con.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]


def execute(query: str, params: Iterable[Any] = ()) -> int:
    with db_connection() as con:
        if USE_POSTGRES:
            translated = postgres_query(query)
            clean = translated.strip().rstrip(";")
            if clean.upper().startswith("INSERT ") and "RETURNING " not in clean.upper():
                row = con.execute(clean + " RETURNING id", tuple(params)).fetchone()
                if row and row.get("id") is not None:
                    return int(row["id"])
                return 0
            con.execute(translated, tuple(params))
            return 0

        cursor = con.execute(query, tuple(params))
        return int(cursor.lastrowid or 0)


def execute_many(query: str, rows: Iterable[Iterable[Any]]) -> None:
    row_list = [tuple(row) for row in rows]
    if not row_list:
        return
    with db_connection() as con:
        if USE_POSTGRES:
            con.executemany(postgres_query(query), row_list)
        else:
            con.executemany(query, row_list)


def init_database() -> None:
    """Create the local SQLite schema or the hosted Postgres schema."""
    if USE_POSTGRES:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS companies (
                id SERIAL PRIMARY KEY,
                owner_key TEXT UNIQUE NOT NULL DEFAULT 'local-demo',
                company_name TEXT DEFAULT '',
                contact_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                website TEXT DEFAULT '',
                service_area TEXT DEFAULT '',
                main_services TEXT DEFAULT '',
                booking_link TEXT DEFAULT '',
                review_link TEXT DEFAULT '',
                preferred_tone TEXT DEFAULT 'Professional',
                sender_name TEXT DEFAULT '',
                sender_email TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                client_name TEXT NOT NULL,
                project_type TEXT DEFAULT '',
                lead_source TEXT DEFAULT 'Unknown',
                client_email TEXT DEFAULT '',
                client_phone TEXT DEFAULT '',
                preferred_contact_method TEXT DEFAULT 'Email',
                project_address TEXT DEFAULT '',
                budget TEXT DEFAULT '',
                timeline TEXT DEFAULT '',
                estimated_value TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                status TEXT DEFAULT 'New',
                reason_lost TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                closed_at TEXT DEFAULT '',
                last_contact_date TEXT DEFAULT '',
                next_followup_date TEXT DEFAULT '',
                followup_priority TEXT DEFAULT 'Medium',
                followup_notes TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lead_events (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS outputs (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
                output_type TEXT NOT NULL,
                client_name TEXT DEFAULT '',
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS proposal_versions (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                client_name TEXT DEFAULT '',
                project_type TEXT DEFAULT '',
                version_number INTEGER NOT NULL,
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                lead_id INTEGER UNIQUE REFERENCES leads(id) ON DELETE SET NULL,
                project_name TEXT DEFAULT '',
                client_name TEXT DEFAULT '',
                project_type TEXT DEFAULT '',
                client_email TEXT DEFAULT '',
                client_phone TEXT DEFAULT '',
                project_address TEXT DEFAULT '',
                estimated_value TEXT DEFAULT '',
                current_stage TEXT DEFAULT 'Won / Handoff',
                project_status TEXT DEFAULT 'Active',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_events (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                related_type TEXT DEFAULT '',
                related_id INTEGER,
                category TEXT DEFAULT '',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                due_date TEXT DEFAULT '',
                status TEXT DEFAULT 'Pending',
                priority TEXT DEFAULT 'Medium',
                subject TEXT DEFAULT '',
                generated_content TEXT DEFAULT '',
                generated_at TEXT DEFAULT '',
                sent_at TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                owner_key TEXT DEFAULT '',
                feedback_type TEXT DEFAULT '',
                rating INTEGER,
                message TEXT NOT NULL,
                contact_email TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)",
            "CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_company ON tasks(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id)",
        ]
        with db_connection() as con:
            for statement in statements:
                con.execute(statement)
        return

    with db_connection() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY,
                owner_key TEXT UNIQUE DEFAULT 'local-demo',
                company_name TEXT DEFAULT '',
                contact_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                website TEXT DEFAULT '',
                service_area TEXT DEFAULT '',
                main_services TEXT DEFAULT '',
                booking_link TEXT DEFAULT '',
                review_link TEXT DEFAULT '',
                preferred_tone TEXT DEFAULT 'Professional',
                sender_name TEXT DEFAULT '',
                sender_email TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                project_type TEXT DEFAULT '',
                lead_source TEXT DEFAULT 'Unknown',
                client_email TEXT DEFAULT '',
                client_phone TEXT DEFAULT '',
                preferred_contact_method TEXT DEFAULT 'Email',
                project_address TEXT DEFAULT '',
                budget TEXT DEFAULT '',
                timeline TEXT DEFAULT '',
                estimated_value TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                status TEXT DEFAULT 'New',
                reason_lost TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                closed_at TEXT DEFAULT '',
                last_contact_date TEXT DEFAULT '',
                next_followup_date TEXT DEFAULT '',
                followup_priority TEXT DEFAULT 'Medium',
                followup_notes TEXT DEFAULT '',
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS lead_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                lead_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                lead_id INTEGER,
                output_type TEXT NOT NULL,
                client_name TEXT DEFAULT '',
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                lead_id INTEGER NOT NULL,
                client_name TEXT DEFAULT '',
                project_type TEXT DEFAULT '',
                version_number INTEGER NOT NULL,
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                lead_id INTEGER UNIQUE,
                project_name TEXT DEFAULT '',
                client_name TEXT DEFAULT '',
                project_type TEXT DEFAULT '',
                client_email TEXT DEFAULT '',
                client_phone TEXT DEFAULT '',
                project_address TEXT DEFAULT '',
                estimated_value TEXT DEFAULT '',
                current_stage TEXT DEFAULT 'Won / Handoff',
                project_status TEXT DEFAULT 'Active',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT DEFAULT '',
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS project_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                related_type TEXT DEFAULT '',
                related_id INTEGER,
                category TEXT DEFAULT '',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                due_date TEXT DEFAULT '',
                status TEXT DEFAULT 'Pending',
                priority TEXT DEFAULT 'Medium',
                subject TEXT DEFAULT '',
                generated_content TEXT DEFAULT '',
                generated_at TEXT DEFAULT '',
                sent_at TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                owner_key TEXT DEFAULT '',
                feedback_type TEXT DEFAULT '',
                rating INTEGER,
                message TEXT NOT NULL,
                contact_email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_company ON tasks(company_id);
            CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id);
            """
        )


def ensure_schema_migrations() -> None:
    """Safely add beta columns/tables when an older local database already exists."""
    if USE_POSTGRES:
        with db_connection() as con:
            con.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS owner_key TEXT")
            con.execute("UPDATE companies SET owner_key = 'local-demo' WHERE owner_key IS NULL OR owner_key = ''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_owner_key ON companies(owner_key)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id SERIAL PRIMARY KEY,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    owner_key TEXT DEFAULT '',
                    feedback_type TEXT DEFAULT '',
                    rating INTEGER,
                    message TEXT NOT NULL,
                    contact_email TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id)")
        return

    company_columns = {row.get("name") for row in fetch_all("PRAGMA table_info(companies)")}
    if "owner_key" not in company_columns:
        execute("ALTER TABLE companies ADD COLUMN owner_key TEXT DEFAULT 'local-demo'")
    execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_owner_key ON companies(owner_key)")
    execute("UPDATE companies SET owner_key = 'local-demo' WHERE owner_key IS NULL OR owner_key = ''")

    # feedback is also created by init_database, but this protects older databases.
    with db_connection() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                owner_key TEXT DEFAULT '',
                feedback_type TEXT DEFAULT '',
                rating INTEGER,
                message TEXT NOT NULL,
                contact_email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id)")


def streamlit_auth_is_available() -> bool:
    return hasattr(st, "user") and hasattr(st, "login") and hasattr(st, "logout")


def auth_is_configured() -> bool:
    if not AUTH_REQUIRED:
        return False
    try:
        # Streamlit OIDC requires an [auth] config in secrets.toml / Cloud secrets.
        return bool(st.secrets.get("auth"))
    except Exception:
        return False


def require_auth_if_enabled() -> dict[str, str]:
    """
    In local mode the app works exactly like your current tester build.
    In beta mode, set BUILDERFLOW_AUTH_REQUIRED=true and configure Streamlit OIDC secrets.
    """
    if not AUTH_REQUIRED:
        return {
            "owner_key": "local-demo",
            "label": "Local Demo",
            "email": "",
        }

    if not streamlit_auth_is_available():
        st.error("Authentication mode is on, but this Streamlit version does not support st.login/st.user yet. Upgrade Streamlit first.")
        st.stop()

    if not auth_is_configured():
        st.error("Authentication mode is on, but the [auth] settings are missing from Streamlit secrets.")
        st.caption("Add your OIDC settings to .streamlit/secrets.toml locally or the Cloud Secrets panel when deployed.")
        st.stop()

    if not st.user.is_logged_in:
        st.markdown(
            "<div class='bf-card'><div class='bf-section-label'>BuilderFlow Beta Login</div>"
            "Log in to open your private company workspace.</div>",
            unsafe_allow_html=True,
        )
        st.button("Log in", on_click=st.login)
        st.stop()

    user_email = ""
    user_sub = ""
    user_name = ""

    try:
        user_email = str(st.user.get("email", "") or "")
        user_sub = str(st.user.get("sub", "") or "")
        user_name = str(st.user.get("name", "") or "")
    except Exception:
        user_email = str(getattr(st.user, "email", "") or "")
        user_sub = str(getattr(st.user, "sub", "") or "")
        user_name = str(getattr(st.user, "name", "") or "")

    owner_key = user_email or user_sub or "authenticated-user"
    label = user_name or user_email or "Authenticated User"

    return {
        "owner_key": owner_key,
        "label": label,
        "email": user_email,
    }


def ensure_company_for_owner(owner_key: str, label: str = "") -> int:
    existing = fetch_one("SELECT id FROM companies WHERE owner_key = ?", (owner_key,))
    if existing:
        return int(existing["id"])

    company_name = "" if owner_key == "local-demo" else f"{label}'s Workspace".strip()
    company_id = execute(
        """
        INSERT INTO companies (
            owner_key, company_name, contact_name, phone, email, website,
            service_area, main_services, booking_link, review_link,
            preferred_tone, sender_name, sender_email, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_key,
            company_name,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "Professional",
            "",
            "",
            now_string(),
        ),
    )

    if company_id:
        return int(company_id)

    fallback = fetch_one("SELECT id FROM companies WHERE owner_key = ?", (owner_key,))
    return int((fallback or {}).get("id", LOCAL_COMPANY_ID))


def storage_mode_label() -> str:
    return "Cloud Postgres" if USE_POSTGRES else "Local SQLite"


def authentication_mode_label() -> str:
    return "Login Required" if AUTH_REQUIRED else "Local Testing Mode"


def database_has_user_data() -> bool:
    lead_count = fetch_one("SELECT COUNT(*) AS count FROM leads WHERE company_id = ?", (ACTIVE_COMPANY_ID,))
    output_count = fetch_one("SELECT COUNT(*) AS count FROM outputs WHERE company_id = ?", (ACTIVE_COMPANY_ID,))
    return bool((lead_count or {}).get("count", 0) or (output_count or {}).get("count", 0))


def migrate_legacy_json_if_needed() -> None:
    if not LEGACY_JSON_FILE.exists() or database_has_user_data():
        return

    try:
        with open(LEGACY_JSON_FILE, "r", encoding="utf-8") as file:
            legacy = json.load(file)
    except Exception:
        return

    profile = legacy.get("company_profile", {}) or {}
    save_company_profile({
        "company_name": profile.get("company_name", ""),
        "contact_name": profile.get("contact_name", ""),
        "phone": profile.get("phone", ""),
        "email": profile.get("email", ""),
        "website": profile.get("website", ""),
        "service_area": profile.get("service_area", ""),
        "main_services": profile.get("main_services", ""),
        "booking_link": profile.get("booking_link", ""),
        "review_link": profile.get("review_link", ""),
        "preferred_tone": profile.get("preferred_tone", "Professional"),
        "sender_name": profile.get("sender_name", ""),
        "sender_email": profile.get("sender_email", ""),
    })

    lead_id_map: dict[int, int] = {}
    for old_lead in legacy.get("leads", []) or []:
        new_id = add_lead(
            client_name=old_lead.get("client_name", "Unnamed Lead"),
            project_type=old_lead.get("project_type", ""),
            lead_source=old_lead.get("lead_source", "Unknown"),
            client_email=old_lead.get("client_email", ""),
            client_phone=old_lead.get("client_phone", ""),
            preferred_contact_method=old_lead.get("preferred_contact_method", "Email"),
            project_address=old_lead.get("project_address", ""),
            budget=old_lead.get("budget", ""),
            timeline=old_lead.get("timeline", ""),
            estimated_value=old_lead.get("estimated_value", ""),
            notes=old_lead.get("notes", ""),
            last_contact_date=old_lead.get("last_contact_date", today_string()),
            next_followup_date=old_lead.get("next_followup_date", future_date_string(2)),
            followup_priority=old_lead.get("followup_priority", "Medium"),
            followup_notes=old_lead.get("followup_notes", ""),
            status=old_lead.get("status", "New"),
            reason_lost=old_lead.get("reason_lost", ""),
            created_at=old_lead.get("created_at", now_string()),
            closed_at=old_lead.get("closed_at", ""),
            create_event=False,
        )
        lead_id_map[int(old_lead.get("id", 0))] = new_id

    for event in legacy.get("lead_events", []) or []:
        mapped_lead_id = lead_id_map.get(int(event.get("lead_id", 0)))
        if mapped_lead_id:
            add_lead_event(
                mapped_lead_id,
                event.get("event_type", "Migrated Event"),
                event.get("note", ""),
                created_at=event.get("created_at", now_string()),
            )

    for output in legacy.get("outputs", []) or []:
        mapped_lead_id = lead_id_map.get(int(output.get("lead_id", 0))) if output.get("lead_id") else None
        add_output(
            output_type=output.get("type", output.get("output_type", "Migrated Output")),
            client_name=output.get("client_name", ""),
            content=output.get("content", ""),
            lead_id=mapped_lead_id,
            created_at=output.get("created_at", now_string()),
            create_event=False,
        )

    for proposal in legacy.get("proposal_versions", []) or []:
        mapped_lead_id = lead_id_map.get(int(proposal.get("lead_id", 0)))
        if mapped_lead_id:
            add_proposal_version(
                mapped_lead_id,
                proposal.get("client_name", ""),
                proposal.get("project_type", ""),
                proposal.get("content", ""),
                created_at=proposal.get("created_at", now_string()),
                create_event=False,
            )

    for task in legacy.get("followup_tasks", []) or []:
        mapped_lead_id = lead_id_map.get(int(task.get("lead_id", 0)))
        if mapped_lead_id:
            create_task(
                related_type="lead",
                related_id=mapped_lead_id,
                category="proposal_followup",
                title=task.get("step_name", "Migrated Follow-Up"),
                description=task.get("goal", ""),
                due_date=task.get("due_date", today_string()),
                status=task.get("status", "Pending"),
                subject=task.get("subject", ""),
                generated_content=task.get("generated_content", ""),
                generated_at=task.get("generated_at", ""),
                sent_at=task.get("sent_at", ""),
                last_error=task.get("last_error", ""),
                metadata={"migrated": True},
                created_at=task.get("created_at", now_string()),
            )


# ============================================================
# Company Functions
# ============================================================

def get_company_profile() -> dict[str, Any]:
    return fetch_one("SELECT * FROM companies WHERE id = ?", (ACTIVE_COMPANY_ID,)) or {}


def save_company_profile(profile: dict[str, Any]) -> None:
    execute(
        """
        UPDATE companies
        SET company_name = ?, contact_name = ?, phone = ?, email = ?, website = ?,
            service_area = ?, main_services = ?, booking_link = ?, review_link = ?,
            preferred_tone = ?, sender_name = ?, sender_email = ?
        WHERE id = ?
        """,
        (
            normalize_optional_text(profile.get("company_name")),
            normalize_optional_text(profile.get("contact_name")),
            normalize_optional_text(profile.get("phone")),
            normalize_optional_text(profile.get("email")),
            normalize_optional_text(profile.get("website")),
            normalize_optional_text(profile.get("service_area")),
            normalize_optional_text(profile.get("main_services")),
            normalize_optional_text(profile.get("booking_link")),
            normalize_optional_text(profile.get("review_link")),
            normalize_optional_text(profile.get("preferred_tone") or "Professional"),
            normalize_optional_text(profile.get("sender_name")),
            normalize_optional_text(profile.get("sender_email")),
            ACTIVE_COMPANY_ID,
        ),
    )


def get_company_signature(profile: dict[str, Any]) -> str:
    lines = []
    for key in ["contact_name", "company_name", "phone", "email", "website"]:
        value = normalize_optional_text(profile.get(key)).strip()
        if value:
            lines.append(value)
    return "\n".join(lines) if lines else "[Your Name]\n[Your Company Name]"


def company_context(profile: dict[str, Any]) -> str:
    return f"""
Company profile:
- Company name: {profile.get('company_name', '')}
- Contact person: {profile.get('contact_name', '')}
- Phone: {profile.get('phone', '')}
- Email: {profile.get('email', '')}
- Website: {profile.get('website', '')}
- Service area: {profile.get('service_area', '')}
- Main services: {profile.get('main_services', '')}
- Booking link: {profile.get('booking_link', '')}
- Review link: {profile.get('review_link', '')}
- Preferred tone: {profile.get('preferred_tone', 'Professional')}
"""


# ============================================================
# Lead Functions
# ============================================================

def get_leads() -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM leads WHERE company_id = ? ORDER BY id DESC", (ACTIVE_COMPANY_ID,))


def get_lead_by_id(lead_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM leads WHERE id = ? AND company_id = ?", (lead_id, ACTIVE_COMPANY_ID))


def add_lead(
    client_name: str,
    project_type: str,
    lead_source: str,
    client_email: str,
    client_phone: str,
    preferred_contact_method: str,
    project_address: str,
    budget: str,
    timeline: str,
    estimated_value: str,
    notes: str,
    last_contact_date: str,
    next_followup_date: str,
    followup_priority: str,
    followup_notes: str,
    status: str = "New",
    reason_lost: str = "",
    created_at: str | None = None,
    closed_at: str = "",
    create_event: bool = True,
) -> int:
    lead_id = execute(
        """
        INSERT INTO leads (
            company_id, client_name, project_type, lead_source, client_email, client_phone,
            preferred_contact_method, project_address, budget, timeline, estimated_value,
            notes, status, reason_lost, created_at, closed_at, last_contact_date,
            next_followup_date, followup_priority, followup_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ACTIVE_COMPANY_ID,
            client_name.strip(),
            project_type,
            lead_source,
            client_email.strip(),
            client_phone.strip(),
            preferred_contact_method,
            project_address.strip(),
            budget,
            timeline,
            estimated_value,
            notes,
            status,
            reason_lost,
            created_at or now_string(),
            closed_at,
            str(last_contact_date),
            str(next_followup_date),
            followup_priority,
            followup_notes,
        ),
    )

    if create_event:
        add_lead_event(lead_id, "Lead Created", f"Lead created for {client_name} — {project_type}. Source: {lead_source}.")

    return lead_id


def add_lead_event(lead_id: int, event_type: str, note: str, created_at: str | None = None) -> None:
    execute(
        """
        INSERT INTO lead_events (company_id, lead_id, event_type, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ACTIVE_COMPANY_ID, lead_id, event_type, note, created_at or now_string()),
    )


def get_lead_events(lead_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM lead_events WHERE lead_id = ? AND company_id = ? ORDER BY id DESC",
        (lead_id, ACTIVE_COMPANY_ID),
    )


def edit_lead_details(
    lead_id: int,
    client_name: str,
    project_type: str,
    lead_source: str,
    client_email: str,
    client_phone: str,
    preferred_contact_method: str,
    project_address: str,
    budget: str,
    timeline: str,
    estimated_value: str,
    notes: str,
) -> None:
    execute(
        """
        UPDATE leads
        SET client_name = ?, project_type = ?, lead_source = ?, client_email = ?,
            client_phone = ?, preferred_contact_method = ?, project_address = ?,
            budget = ?, timeline = ?, estimated_value = ?, notes = ?
        WHERE id = ? AND company_id = ?
        """,
        (
            client_name.strip(),
            project_type,
            lead_source,
            client_email.strip(),
            client_phone.strip(),
            preferred_contact_method,
            project_address.strip(),
            budget,
            timeline,
            estimated_value,
            notes,
            lead_id,
            ACTIVE_COMPANY_ID,
        ),
    )
    add_lead_event(lead_id, "Lead Details Edited", "Core lead/project details were updated.")


def update_lead_status(
    lead_id: int,
    status: str,
    reason_lost: str,
    last_contact_date: str,
    next_followup_date: str,
    followup_priority: str,
    followup_notes: str,
) -> None:
    lead = get_lead_by_id(lead_id)
    if not lead:
        return

    closed_at = lead.get("closed_at", "")
    if status in ["Won", "Lost"]:
        closed_at = now_string()
    elif status not in ["Won", "Lost"]:
        closed_at = ""

    execute(
        """
        UPDATE leads
        SET status = ?, reason_lost = ?, last_contact_date = ?, next_followup_date = ?,
            followup_priority = ?, followup_notes = ?, closed_at = ?
        WHERE id = ? AND company_id = ?
        """,
        (
            status,
            reason_lost,
            str(last_contact_date),
            str(next_followup_date),
            followup_priority,
            followup_notes,
            closed_at,
            lead_id,
            ACTIVE_COMPANY_ID,
        ),
    )

    add_lead_event(
        lead_id,
        "Lead Updated",
        f"Status updated to {status}. Next follow-up set for {next_followup_date}.",
    )

    if status == "Won":
        create_project_from_won_lead(lead_id)


def close_lead_from_details(lead_id: int, status: str, closing_note: str) -> None:
    lead = get_lead_by_id(lead_id)
    if not lead:
        return

    update_lead_status(
        lead_id=lead_id,
        status=status,
        reason_lost=closing_note,
        last_contact_date=today_string(),
        next_followup_date=today_string(),
        followup_priority=lead.get("followup_priority", "Medium"),
        followup_notes=lead.get("followup_notes", ""),
    )
    add_lead_event(lead_id, f"Lead Closed — {status}", closing_note or f"Lead closed as {status}.")


def reopen_lead(lead_id: int, reopen_note: str) -> None:
    execute(
        """
        UPDATE leads
        SET status = 'Contacted', reason_lost = '', closed_at = '',
            last_contact_date = ?, next_followup_date = ?
        WHERE id = ? AND company_id = ?
        """,
        (today_string(), future_date_string(2), lead_id, ACTIVE_COMPANY_ID),
    )
    add_lead_event(lead_id, "Lead Reopened", reopen_note or "Lead reopened.")


def delete_lead(lead_id: int) -> None:
    execute("DELETE FROM leads WHERE id = ? AND company_id = ?", (lead_id, ACTIVE_COMPANY_ID))


def mark_followup_generated(lead_id: int, sequence_name: str) -> None:
    execute(
        """
        UPDATE leads
        SET status = 'Follow-Up Sent', last_contact_date = ?, next_followup_date = ?
        WHERE id = ? AND company_id = ?
        """,
        (today_string(), future_date_string(3), lead_id, ACTIVE_COMPANY_ID),
    )
    add_lead_event(lead_id, "Follow-Up Generated", f"A follow-up message was generated using: {sequence_name}.")


def mark_proposal_generated(lead_id: int) -> None:
    execute(
        """
        UPDATE leads
        SET status = 'Proposal Sent', last_contact_date = ?, next_followup_date = ?
        WHERE id = ? AND company_id = ?
        """,
        (today_string(), future_date_string(2), lead_id, ACTIVE_COMPANY_ID),
    )
    add_lead_event(lead_id, "Proposal Generated", "A proposal draft was generated and the lead was marked as Proposal Sent.")


def get_lead_reminder_status(lead: dict[str, Any]) -> str:
    if lead.get("status") in ["Won", "Lost"]:
        return "Closed"
    due = parse_iso_date(lead.get("next_followup_date"))
    today = date.today()
    if due < today:
        return "Overdue"
    if due == today:
        return "Due Today"
    if due <= today + timedelta(days=7):
        return "Upcoming"
    return "Future"


def leads_to_dataframe(leads: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for lead in leads:
        rows.append(
            {
                "ID": lead.get("id"),
                "Client": lead.get("client_name", ""),
                "Project Type": lead.get("project_type", ""),
                "Lead Source": lead.get("lead_source", "Unknown"),
                "Email": lead.get("client_email", ""),
                "Phone": lead.get("client_phone", ""),
                "Preferred Contact": lead.get("preferred_contact_method", "Email"),
                "Project Address": lead.get("project_address", ""),
                "Estimated Value": safe_float(lead.get("estimated_value", 0)),
                "Status": lead.get("status", "New"),
                "Reminder": get_lead_reminder_status(lead),
                "Priority": lead.get("followup_priority", "Medium"),
                "Last Contact": lead.get("last_contact_date", ""),
                "Next Follow-Up": lead.get("next_followup_date", ""),
                "Budget": lead.get("budget", ""),
                "Timeline": lead.get("timeline", ""),
                "Created At": lead.get("created_at", ""),
                "Closed At": lead.get("closed_at", ""),
            }
        )
    return pd.DataFrame(rows)


# ============================================================
# Output + Proposal Functions
# ============================================================

def add_output(
    output_type: str,
    client_name: str,
    content: str,
    lead_id: int | None = None,
    created_at: str | None = None,
    create_event: bool = True,
) -> int:
    output_id = execute(
        """
        INSERT INTO outputs (company_id, lead_id, output_type, client_name, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ACTIVE_COMPANY_ID, lead_id, output_type, client_name, content, created_at or now_string()),
    )
    if lead_id and create_event:
        add_lead_event(lead_id, output_type, f"Generated output: {output_type}.")
    return output_id


def get_outputs() -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM outputs WHERE company_id = ? ORDER BY id DESC", (ACTIVE_COMPANY_ID,))


def get_outputs_for_lead(lead_id: int, client_name: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM outputs
        WHERE company_id = ? AND (lead_id = ? OR (lead_id IS NULL AND LOWER(client_name) = LOWER(?)))
        ORDER BY id DESC
        """,
        (ACTIVE_COMPANY_ID, lead_id, client_name),
    )


def clear_outputs() -> None:
    execute("DELETE FROM outputs WHERE company_id = ?", (ACTIVE_COMPANY_ID,))



def add_feedback(feedback_type: str, rating: int, message: str, contact_email: str = "") -> None:
    execute(
        """
        INSERT INTO feedback (
            company_id, owner_key, feedback_type, rating, message, contact_email, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ACTIVE_COMPANY_ID,
            ACTIVE_OWNER_KEY,
            feedback_type,
            rating,
            message,
            contact_email,
            now_string(),
        ),
    )


def get_onboarding_steps(
    profile: dict[str, Any],
    leads: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outputs = get_outputs()
    has_company = bool(profile.get("company_name") and profile.get("contact_name"))
    has_email = email_sending_ready(profile)
    has_lead = bool(leads)
    has_proposal = any(output.get("output_type") == "Proposal Draft" for output in outputs)
    has_automation = any(task.get("category") == "proposal_followup" for task in tasks)
    has_project = bool(projects)
    feedback_count = fetch_one(
        "SELECT COUNT(*) AS count FROM feedback WHERE company_id = ?",
        (ACTIVE_COMPANY_ID,),
    )
    has_feedback = bool((feedback_count or {}).get("count", 0))

    return [
        {
            "label": "Complete company profile",
            "done": has_company,
            "why": "Personalizes proposals, follow-ups, and signatures.",
        },
        {
            "label": "Configure email sending",
            "done": has_email,
            "why": "Allows BuilderFlow to send real client messages.",
        },
        {
            "label": "Add your first lead",
            "done": has_lead,
            "why": "Starts the pipeline tracking flow.",
        },
        {
            "label": "Generate a proposal draft",
            "done": has_proposal,
            "why": "Tests the proposal + PDF process.",
        },
        {
            "label": "Create the 5-touch follow-up automation",
            "done": has_automation,
            "why": "Confirms proposal follow-up tasks are scheduled.",
        },
        {
            "label": "Convert a won lead into a project",
            "done": has_project,
            "why": "Tests the post-sale workflow.",
        },
        {
            "label": "Use the feedback page after testing",
            "done": has_feedback,
            "why": "Captures beta tester friction and feature ideas.",
        },
    ]


def render_onboarding_checklist(
    profile: dict[str, Any],
    leads: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> None:
    onboarding = get_onboarding_steps(profile, leads, projects, tasks)
    completed = sum(1 for step in onboarding if step["done"])
    total = len(onboarding)
    progress = completed / total if total else 0.0

    st.markdown(
        "<div class='bf-card'><div class='bf-section-label'>Beta Readiness Checklist</div>"
        "Use this to make sure the app works end-to-end before inviting contractors.</div>",
        unsafe_allow_html=True,
    )
    st.progress(progress)
    st.caption(f"{completed} of {total} core beta-readiness checks completed.")

    with st.expander("Open onboarding checklist", expanded=completed < total):
        for step in onboarding:
            icon = "✅" if step["done"] else "⬜"
            st.markdown(f"{icon} **{step['label']}**")
            st.caption(step["why"])


def add_proposal_version(
    lead_id: int,
    client_name: str,
    project_type: str,
    content: str,
    created_at: str | None = None,
    create_event: bool = True,
) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS count FROM proposal_versions WHERE company_id = ? AND lead_id = ?",
        (ACTIVE_COMPANY_ID, lead_id),
    )
    version_number = int((row or {}).get("count", 0)) + 1
    proposal_id = execute(
        """
        INSERT INTO proposal_versions (
            company_id, lead_id, client_name, project_type, version_number, content, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ACTIVE_COMPANY_ID, lead_id, client_name, project_type, version_number, content, created_at or now_string()),
    )
    if create_event:
        add_lead_event(lead_id, "Proposal Version Saved", f"Proposal version {version_number} was saved for {client_name}.")
    return proposal_id


def get_proposal_versions(lead_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM proposal_versions WHERE company_id = ? AND lead_id = ? ORDER BY version_number DESC",
        (ACTIVE_COMPANY_ID, lead_id),
    )


# ============================================================
# Project Functions
# ============================================================

PROJECT_STAGES = [
    "Won / Handoff",
    "Deposit Received",
    "Scheduled",
    "In Progress",
    "Waiting on Client",
    "Completed",
    "Review Requested",
    "Referral Requested",
]


def get_projects() -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM projects WHERE company_id = ? ORDER BY id DESC", (ACTIVE_COMPANY_ID,))


def get_project_by_id(project_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM projects WHERE id = ? AND company_id = ?", (project_id, ACTIVE_COMPANY_ID))


def get_project_by_lead_id(lead_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM projects WHERE lead_id = ? AND company_id = ?", (lead_id, ACTIVE_COMPANY_ID))


def create_project_from_won_lead(lead_id: int) -> int | None:
    existing = get_project_by_lead_id(lead_id)
    if existing:
        return int(existing["id"])

    lead = get_lead_by_id(lead_id)
    if not lead:
        return None

    project_id = execute(
        """
        INSERT INTO projects (
            company_id, lead_id, project_name, client_name, project_type, client_email,
            client_phone, project_address, estimated_value, current_stage,
            project_status, notes, created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Won / Handoff', 'Active', ?, ?, '')
        """,
        (
            ACTIVE_COMPANY_ID,
            lead_id,
            f"{lead.get('client_name', '')} — {lead.get('project_type', '')}",
            lead.get("client_name", ""),
            lead.get("project_type", ""),
            lead.get("client_email", ""),
            lead.get("client_phone", ""),
            lead.get("project_address", ""),
            lead.get("estimated_value", ""),
            lead.get("notes", ""),
            now_string(),
        ),
    )

    add_project_event(project_id, "Project Created", "Project created automatically because the lead was marked Won.")
    create_task(
        related_type="project",
        related_id=project_id,
        category="project_kickoff",
        title="Confirm deposit / kickoff requirements",
        description="Confirm deposit, next paperwork, and handoff into the active project schedule.",
        due_date=today_string(),
    )
    return project_id


def add_project_event(project_id: int, event_type: str, note: str, created_at: str | None = None) -> None:
    execute(
        """
        INSERT INTO project_events (company_id, project_id, event_type, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ACTIVE_COMPANY_ID, project_id, event_type, note, created_at or now_string()),
    )


def get_project_events(project_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM project_events WHERE company_id = ? AND project_id = ? ORDER BY id DESC",
        (ACTIVE_COMPANY_ID, project_id),
    )


def update_project_stage(project_id: int, new_stage: str, note: str = "") -> None:
    project = get_project_by_id(project_id)
    if not project:
        return

    project_status = project.get("project_status", "Active")
    completed_at = project.get("completed_at", "")

    if new_stage == "Completed":
        project_status = "Completed"
        completed_at = now_string()
    elif new_stage not in ["Review Requested", "Referral Requested"]:
        project_status = "Active"
        completed_at = ""

    execute(
        """
        UPDATE projects
        SET current_stage = ?, project_status = ?, completed_at = ?
        WHERE id = ? AND company_id = ?
        """,
        (new_stage, project_status, completed_at, project_id, ACTIVE_COMPANY_ID),
    )

    add_project_event(project_id, "Project Stage Updated", note or f"Stage changed to {new_stage}.")

    if new_stage == "Completed":
        ensure_post_completion_tasks(project_id)


def edit_project_notes(project_id: int, notes: str) -> None:
    execute(
        "UPDATE projects SET notes = ? WHERE id = ? AND company_id = ?",
        (notes, project_id, ACTIVE_COMPANY_ID),
    )
    add_project_event(project_id, "Project Notes Updated", "Project notes were edited.")


def projects_to_dataframe(projects: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for project in projects:
        rows.append(
            {
                "ID": project.get("id"),
                "Project": project.get("project_name", ""),
                "Client": project.get("client_name", ""),
                "Type": project.get("project_type", ""),
                "Stage": project.get("current_stage", ""),
                "Status": project.get("project_status", ""),
                "Value": safe_float(project.get("estimated_value", "")),
                "Created": project.get("created_at", ""),
                "Completed": project.get("completed_at", ""),
            }
        )
    return pd.DataFrame(rows)


# ============================================================
# Unified Task Center + Automation Functions
# ============================================================

AUTOMATION_STEPS = [
    {
        "title": "Day 0 — Proposal Check-In",
        "offset": 0,
        "goal": "Ask whether anything should be adjusted before the client decides.",
    },
    {
        "title": "Day 2 — Quick Follow-Up",
        "offset": 2,
        "goal": "Check in after proposal delivery and offer to answer questions.",
    },
    {
        "title": "Day 5 — Option Review",
        "offset": 5,
        "goal": "Offer option A/B or good-better-best scope/pricing conversation.",
    },
    {
        "title": "Day 10 — Decision Prompt",
        "offset": 10,
        "goal": "Ask whether the project is a yes, no, or later.",
    },
    {
        "title": "Day 21 — Final Check-In",
        "offset": 21,
        "goal": "Final check-in; if later, keep the relationship warm.",
    },
]


def create_task(
    related_type: str,
    related_id: int,
    category: str,
    title: str,
    description: str,
    due_date: str,
    status: str = "Pending",
    subject: str = "",
    generated_content: str = "",
    generated_at: str = "",
    sent_at: str = "",
    completed_at: str = "",
    last_error: str = "",
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    return execute(
        """
        INSERT INTO tasks (
            company_id, related_type, related_id, category, title, description, due_date,
            status, subject, generated_content, generated_at, sent_at, completed_at,
            last_error, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ACTIVE_COMPANY_ID,
            related_type,
            related_id,
            category,
            title,
            description,
            due_date,
            status,
            subject,
            generated_content,
            generated_at,
            sent_at,
            completed_at,
            last_error,
            json.dumps(metadata or {}),
            created_at or now_string(),
        ),
    )


def get_tasks() -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM tasks WHERE company_id = ? ORDER BY due_date ASC, id ASC", (ACTIVE_COMPANY_ID,))


def get_task_by_id(task_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM tasks WHERE id = ? AND company_id = ?", (task_id, ACTIVE_COMPANY_ID))


def get_tasks_for_lead(lead_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM tasks
        WHERE company_id = ? AND related_type = 'lead' AND related_id = ?
        ORDER BY due_date ASC, id ASC
        """,
        (ACTIVE_COMPANY_ID, lead_id),
    )


def get_tasks_for_project(project_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM tasks
        WHERE company_id = ? AND related_type = 'project' AND related_id = ?
        ORDER BY due_date ASC, id ASC
        """,
        (ACTIVE_COMPANY_ID, project_id),
    )


def task_bucket(task: dict[str, Any]) -> str:
    if task.get("status") in ["Sent", "Done"]:
        return task.get("status", "Done")
    if task.get("status") == "Skipped":
        return "Skipped"

    due = parse_iso_date(task.get("due_date"))
    today = date.today()
    if due < today:
        return "Overdue"
    if due == today:
        return "Due Today"
    if due <= today + timedelta(days=7):
        return "Upcoming"
    return "Future"


def task_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    buckets = {"Overdue": 0, "Due Today": 0, "Upcoming": 0, "Future": 0, "Sent": 0, "Done": 0, "Skipped": 0}
    for task in tasks:
        bucket = task_bucket(task)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return buckets


def mark_task_done(task_id: int) -> None:
    execute(
        "UPDATE tasks SET status = 'Done', completed_at = ? WHERE id = ? AND company_id = ?",
        (now_string(), task_id, ACTIVE_COMPANY_ID),
    )


def mark_task_skipped(task_id: int, note: str = "Skipped by user.") -> None:
    execute(
        "UPDATE tasks SET status = 'Skipped', completed_at = ?, last_error = '' WHERE id = ? AND company_id = ?",
        (now_string(), task_id, ACTIVE_COMPANY_ID),
    )
    task = get_task_by_id(task_id)
    if not task:
        return
    if task.get("related_type") == "lead":
        add_lead_event(task["related_id"], "Task Skipped", f"{task.get('title', '')}: {note}")
    elif task.get("related_type") == "project":
        add_project_event(task["related_id"], "Task Skipped", f"{task.get('title', '')}: {note}")


def create_proposal_followup_tasks(lead_id: int) -> tuple[bool, str]:
    lead = get_lead_by_id(lead_id)
    if not lead:
        return False, "Lead not found."

    active = fetch_one(
        """
        SELECT COUNT(*) AS count FROM tasks
        WHERE company_id = ? AND related_type = 'lead' AND related_id = ?
          AND category = 'proposal_followup'
          AND status IN ('Pending', 'Generated')
        """,
        (ACTIVE_COMPANY_ID, lead_id),
    )
    if int((active or {}).get("count", 0)) > 0:
        return False, "This lead already has active proposal follow-up tasks."

    for step in AUTOMATION_STEPS:
        create_task(
            related_type="lead",
            related_id=lead_id,
            category="proposal_followup",
            title=step["title"],
            description=step["goal"],
            due_date=future_date_string(step["offset"]),
            metadata={"automation_plan": "5-touch proposal follow-up", "offset": step["offset"]},
        )

    add_lead_event(
        lead_id,
        "Automation Scheduled",
        "5-touch proposal follow-up plan scheduled: Day 0, Day 2, Day 5, Day 10, Day 21.",
    )
    return True, "5-touch proposal follow-up plan scheduled."


def ensure_post_completion_tasks(project_id: int) -> None:
    existing_review = fetch_one(
        """
        SELECT COUNT(*) AS count FROM tasks
        WHERE company_id = ? AND related_type = 'project' AND related_id = ?
          AND category = 'review_request'
        """,
        (ACTIVE_COMPANY_ID, project_id),
    )
    existing_referral = fetch_one(
        """
        SELECT COUNT(*) AS count FROM tasks
        WHERE company_id = ? AND related_type = 'project' AND related_id = ?
          AND category = 'referral_request'
        """,
        (ACTIVE_COMPANY_ID, project_id),
    )

    if int((existing_review or {}).get("count", 0)) == 0:
        create_task(
            related_type="project",
            related_id=project_id,
            category="review_request",
            title="Request client review",
            description="Send a review request now that the project is completed.",
            due_date=future_date_string(1),
        )

    if int((existing_referral or {}).get("count", 0)) == 0:
        create_task(
            related_type="project",
            related_id=project_id,
            category="referral_request",
            title="Request referral / testimonial",
            description="Ask for referrals or a testimonial while the completed project is still fresh.",
            due_date=future_date_string(7),
        )


# ============================================================
# AI + Email + PDF
# ============================================================

def has_openai_key() -> bool:
    return bool(read_secret_or_env("OPENAI_API_KEY", "")) and OpenAI is not None


def ai_generate(prompt: str, fallback_text: str) -> str:
    cleaned_fallback = clean_ai_output(fallback_text)

    if not has_openai_key():
        return cleaned_fallback

    try:
        client = OpenAI(api_key=read_secret_or_env("OPENAI_API_KEY", ""))
        final_prompt = prompt + "\n\n" + plain_text_ai_rule()

        with st.spinner("🔨 Building this with BuilderFlow..."):
            response = client.responses.create(
                model=read_secret_or_env("OPENAI_MODEL", "gpt-5.2"),
                input=final_prompt,
            )

        return clean_ai_output(response.output_text)

    except Exception as error:
        return cleaned_fallback + "\n\n---\n" + f"AI error fallback used. Error: {str(error)}"


def has_resend_key() -> bool:
    return bool(read_secret_or_env("RESEND_API_KEY", "")) and RESEND_AVAILABLE


def email_sending_ready(profile: dict[str, Any]) -> bool:
    return has_resend_key() and bool(profile.get("sender_email"))


def send_email_via_resend(to_email: str, subject: str, body_text: str, profile: dict[str, Any]) -> tuple[bool, str]:
    if not RESEND_AVAILABLE:
        return False, "Resend package is not installed. Run: pip install resend"
    resend_key = read_secret_or_env("RESEND_API_KEY", "")
    if not resend_key:
        return False, "RESEND_API_KEY is missing from your local .env file or Streamlit Cloud secrets."
    sender_email = normalize_optional_text(profile.get("sender_email")).strip()
    if not sender_email:
        return False, "Add a Sender Email in Account before sending."
    sender_name = normalize_optional_text(profile.get("sender_name")).strip() or normalize_optional_text(profile.get("company_name")).strip() or "BuilderFlow"
    try:
        resend.api_key = resend_key
        result = resend.Emails.send(
            {
                "from": f"{sender_name} <{sender_email}>",
                "to": [to_email],
                "subject": subject,
                "html": text_to_html(body_text),
            }
        )
        return True, str(result)
    except Exception as error:
        return False, str(error)


def create_proposal_pdf(client_name: str, project_type: str, proposal_text: str, profile: dict[str, Any]) -> bytes | None:
    if not REPORTLAB_AVAILABLE:
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BuilderFlowTitle", parent=styles["Title"], fontSize=20, leading=24, alignment=TA_LEFT, spaceAfter=10)
    section_style = ParagraphStyle("BuilderFlowSection", parent=styles["Heading2"], fontSize=13, leading=16, alignment=TA_LEFT, spaceBefore=10, spaceAfter=6)
    body_style = ParagraphStyle("BuilderFlowBody", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=7)
    footer_style = ParagraphStyle("BuilderFlowFooter", parent=styles["Normal"], fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.grey)

    company_name = profile.get("company_name", "") or "Builder / Remodeler"
    contact_lines = [profile.get("contact_name", ""), profile.get("phone", ""), profile.get("email", ""), profile.get("website", "")]
    contact_block = "<br/>".join(clean_pdf_text(line) for line in contact_lines if line)

    header_data = [[
        Paragraph(f"<b>{clean_pdf_text(company_name)}</b><br/>{contact_block}", body_style),
        Paragraph(f"<b>Prepared For:</b><br/>{clean_pdf_text(client_name)}<br/><br/><b>Date:</b><br/>{datetime.now().strftime('%Y-%m-%d')}", body_style),
    ]]
    header_table = Table(header_data, colWidths=[3.7 * inch, 2.5 * inch])
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    story = [
        Paragraph("Project Proposal", title_style),
        header_table,
        Spacer(1, 0.2 * inch),
        Paragraph(f"<b>Project Type:</b> {clean_pdf_text(project_type)}", body_style),
        Spacer(1, 0.15 * inch),
        Paragraph("Proposal Details", section_style),
    ]

    for paragraph in proposal_text.split("\n"):
        clean_para = paragraph.strip()
        story.append(Paragraph(clean_pdf_text(clean_para), body_style) if clean_para else Spacer(1, 0.06 * inch))

    story.extend(
        [
            Spacer(1, 0.25 * inch),
            Paragraph("This proposal draft is for planning and review. Final pricing, schedule, and scope should be confirmed before approval.", footer_style),
        ]
    )

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ============================================================
# Prompt Builders + Fallbacks
# ============================================================

FOLLOWUP_SEQUENCE_OPTIONS = [
    "Same-Day First Response",
    "2-Day Check-In",
    "7-Day Value Follow-Up",
    "14-Day Final Check-In",
    "Post-Proposal Follow-Up",
    "5-Touch Proposal Follow-Up Plan",
    "Budget Concern Follow-Up",
    "Timeline Concern Follow-Up",
]


def followup_sequence_instruction(sequence_name: str) -> str:
    instructions = {
        "Same-Day First Response": "Write a fast first response after a new inquiry. Goal: start trust, confirm the project, and push toward a call or walkthrough.",
        "2-Day Check-In": "Write a polite check-in after the client has not replied for about two days. Goal: restart the conversation without pressure.",
        "7-Day Value Follow-Up": "Write a value-based follow-up. Include helpful guidance or what the client should think about before starting.",
        "14-Day Final Check-In": "Write a final check-in that politely leaves the door open without sounding desperate.",
        "Post-Proposal Follow-Up": "Write a follow-up after a proposal has been sent. Goal: answer questions, reduce uncertainty, and move toward a decision.",
        "5-Touch Proposal Follow-Up Plan": "Use a structured 5-touch system after an estimate or proposal: Day 0 ask if anything should be adjusted, Day 2 quick check-in, Day 5 offer option A/B or good-better-best pricing, Day 10 ask if it is a yes/no/later, Day 21 final check-in and keep-in-touch if later.",
        "Budget Concern Follow-Up": "Write a follow-up for a client worried about budget. Goal: be honest, helpful, and explain scope/prioritization without discounting aggressively.",
        "Timeline Concern Follow-Up": "Write a follow-up for a client worried about timeline. Goal: explain next steps, planning, schedule clarity, and realistic expectations.",
    }
    return instructions.get(sequence_name, instructions["Same-Day First Response"])


def build_follow_up_prompt(profile: dict[str, Any], client_name: str, project_type: str, budget: str, timeline: str, notes: str, sequence_name: str) -> str:
    return f"""
You are writing for a professional residential remodeler/custom builder.

{company_context(profile)}

Follow-up sequence type: {sequence_name}
Sequence instruction: {followup_sequence_instruction(sequence_name)}

Client name: {client_name}
Project type: {project_type}
Budget: {budget}
Timeline: {timeline}
Notes: {notes}

Rules:
- Use the company profile naturally.
- Match the preferred tone.
- Sound confident, helpful, and professional.
- Do not sound pushy.
- Mention a clear next step.
- Include the booking link if available.
- Keep it under 190 words.
- Sign off using the company contact details.
"""


def build_proposal_prompt(profile: dict[str, Any], client_name: str, project_type: str, budget: str, timeline: str, notes: str) -> str:
    return f"""
You are helping a remodeler/custom builder create a polished proposal draft.

{company_context(profile)}

Create a clean proposal/scope draft using this information.
Client name: {client_name}
Project type: {project_type}
Budget: {budget}
Timeline: {timeline}
Notes/walkthrough details: {notes}

Include:
1. Opening summary
2. Project understanding
3. Proposed scope of work
4. Assumptions
5. Next steps
6. Company signature/contact details

Important:
- Do not invent exact prices.
- Use placeholders where details are missing.
- Make it professional but easy to understand.
- Use the company profile naturally.
"""


def build_client_update_prompt(profile: dict[str, Any], client_name: str, project_name: str, progress_notes: str, blockers: str, next_steps: str) -> str:
    return f"""
You are writing a professional client progress update for a remodeler/custom builder.

{company_context(profile)}
Client name: {client_name}
Project name: {project_name}
Progress notes: {progress_notes}
Blockers/issues: {blockers}
Next steps: {next_steps}

Create a clear client update.
- Match the preferred tone.
- Reassuring, organized, and easy to read.
- Do not overpromise.
- Sign off using company contact details.
"""


def build_referral_prompt(profile: dict[str, Any], client_name: str, project_name: str, result_notes: str) -> str:
    return f"""
You are writing a review/referral request for a remodeler/custom builder.

{company_context(profile)}
Client name: {client_name}
Project name: {project_name}
Result notes: {result_notes}

Create:
1. A short review request message.
2. A short referral request message.
3. A simple testimonial prompt the client can answer.

Use the review link if available. Tone: thankful, professional, not awkward, not pushy.
"""


def build_owner_insight_prompt(profile: dict[str, Any], summary_text: str) -> str:
    return f"""
You are an operations and sales advisor for a remodeler/custom builder.

{company_context(profile)}

Based on this business summary, give concise owner insights.
{summary_text}

Include:
1. What is going well.
2. Where money may be leaking.
3. Which leads/tasks need attention.
4. Which lead sources or project types look strongest.
5. One practical recommendation to improve close rate.
"""


def build_automation_task_prompt(profile: dict[str, Any], lead: dict[str, Any], task: dict[str, Any]) -> str:
    return f"""
You are writing an automated proposal follow-up email for a remodeler/custom builder.

{company_context(profile)}

Follow-up step: {task.get('title', '')}
Goal: {task.get('description', '')}

Lead details:
- Client name: {lead.get('client_name', '')}
- Project type: {lead.get('project_type', '')}
- Budget: {lead.get('budget', '')}
- Timeline: {lead.get('timeline', '')}
- Lead notes: {lead.get('notes', '')}

Return in this exact format:
SUBJECT: ...
BODY:
...

Rules:
- Keep the body under 180 words.
- Match company tone.
- No fake urgency.
- Move the deal forward.
- Sign off with company contact details.
"""


def fallback_follow_up(profile: dict[str, Any], client_name: str, project_type: str, budget: str, timeline: str, notes: str, sequence_name: str) -> str:
    signature = get_company_signature(profile)
    booking_link = profile.get("booking_link", "").strip()
    booking_line = f"\n\nYou can also book a time here: {booking_link}" if booking_link else ""
    return f"""Subject: Following up on your {project_type} project

Hi {client_name},

Thanks again for connecting about your {project_type} project. Based on what you shared, I wanted to keep the conversation moving in a clear, helpful way.

Budget noted: {budget}
Timeline noted: {timeline}

The best next step is to confirm the scope, priorities, and any questions you want addressed before moving forward.

Notes I have so far:
{notes}{booking_line}

Thanks,
{signature}"""


def fallback_proposal(profile: dict[str, Any], client_name: str, project_type: str, budget: str, timeline: str, notes: str) -> str:
    signature = get_company_signature(profile)
    company_name = profile.get("company_name", "") or "[Your Company Name]"
    return f"""Proposal Draft for {client_name}

Prepared by:
{company_name}

Project Type:
{project_type}

Project Summary:
Thank you for the opportunity to review this project. The goal is to complete a professional {project_type} project with clear communication, organized scheduling, and quality workmanship.

Project Understanding:
{notes}

Budget Range:
{budget}

Desired Timeline:
{timeline}

Proposed Scope of Work:
- Confirm project goals and final requirements.
- Complete walkthrough and measurements.
- Review material selections and design preferences.
- Prepare detailed scope and schedule.
- Complete work according to agreed plan.
- Provide client updates throughout the project.

Assumptions:
- Final pricing depends on confirmed measurements, materials, labour requirements, and site conditions.
- Changes to scope may affect cost and schedule.
- Permits, engineering, or specialty trades will be confirmed if required.

Next Steps:
1. Confirm details.
2. Schedule a walkthrough or follow-up meeting.
3. Prepare final estimate/proposal.
4. Confirm start date and deposit requirements.

Prepared by:
{signature}"""


def fallback_client_update(profile: dict[str, Any], client_name: str, project_name: str, progress_notes: str, blockers: str, next_steps: str) -> str:
    signature = get_company_signature(profile)
    return f"""Subject: Project Update — {project_name}

Hi {client_name},

Here is the latest update on your project.

Completed / Progress Made:
{progress_notes}

Items We Are Watching:
{blockers or 'No major issues at this time.'}

Next Steps:
{next_steps}

We will continue keeping things organized and will update you if anything changes with the schedule, materials, or project scope.

Thanks,
{signature}"""


def fallback_referral(profile: dict[str, Any], client_name: str, project_name: str, result_notes: str) -> str:
    signature = get_company_signature(profile)
    review_link = profile.get("review_link", "").strip()
    review_line = f"\n\nReview link: {review_link}" if review_link else ""
    return f"""Review Request:

Hi {client_name},

Thank you again for trusting us with your {project_name} project. It was a pleasure working with you.

If you were happy with the experience, would you be open to leaving us a short review? It helps future clients feel confident choosing us.{review_line}

Thanks again,
{signature}


Referral Request:

Hi {client_name},

If you know any friends, family, or neighbours who are planning a renovation or custom project, we would be grateful if you passed our name along.

Thanks again,
{signature}


Simple Testimonial Prompt:
What was your experience like working with us, and what would you tell someone considering hiring us?

Project result notes:
{result_notes}"""


def fallback_owner_insights(metrics: dict[str, Any]) -> str:
    return f"""Owner Insights

What is going well:
You are tracking leads, values, tasks, and projects in one system. That creates far more visibility than relying on scattered notes.

Where money may be leaking:
Overdue follow-ups, due task backlogs, and no-response leads deserve attention first.

Current snapshot:
- Total leads: {metrics.get('total_leads', 0)}
- Won jobs: {metrics.get('won_count', 0)}
- Lost jobs: {metrics.get('lost_count', 0)}
- Active projects: {metrics.get('active_projects', 0)}
- Tasks due today: {metrics.get('tasks_due_today', 0)}
- Tasks overdue: {metrics.get('tasks_overdue', 0)}
- Win rate: {metrics.get('win_rate', 0):.1f}%

Practical recommendation:
Keep the five-touch proposal follow-up running consistently, then review win rate by lead source and project type monthly."""


def fallback_automation_message(profile: dict[str, Any], lead: dict[str, Any], task: dict[str, Any]) -> tuple[str, str]:
    signature = get_company_signature(profile)
    client_name = lead.get("client_name", "there")
    project_type = lead.get("project_type", "project")
    title = task.get("title", "")

    if "Day 0" in title:
        subject = f"Anything you want adjusted before deciding on your {project_type} proposal?"
        body = f"""Hi {client_name},

I wanted to follow up on the proposal for your {project_type} project. Before you make a decision, is there anything you would like adjusted, clarified, or broken down differently?

I want the scope to feel clear and aligned with what you actually want.

Thanks,
{signature}"""
    elif "Day 2" in title:
        subject = f"Quick check-in on your {project_type} proposal"
        body = f"""Hi {client_name},

Just checking in to see whether you had any questions after reviewing the proposal for your {project_type} project.

I’m happy to walk through the scope, timeline, or any decision points that would help.

Thanks,
{signature}"""
    elif "Day 5" in title:
        subject = f"Would option-based pricing help for your {project_type} project?"
        body = f"""Hi {client_name},

I wanted to check whether it would help to review the project in a few options, such as a simpler version, the current scope, or a more complete version.

That can make it easier to choose the right fit.

Thanks,
{signature}"""
    elif "Day 10" in title:
        subject = f"Should we treat your {project_type} project as yes, no, or later?"
        body = f"""Hi {client_name},

I wanted to touch base and see where things stand with your {project_type} project. Should we treat this as a yes, no, or something to revisit later?

Any answer is helpful so we can plan properly on our side.

Thanks,
{signature}"""
    else:
        subject = f"Final check-in on your {project_type} project"
        body = f"""Hi {client_name},

I wanted to send one final check-in for now regarding your {project_type} project. If the timing is not right yet, that is completely fine.

We would be happy to stay in touch and revisit it when the project becomes a priority again.

Thanks,
{signature}"""

    return subject, body


def parse_generated_subject_body(text: str, profile: dict[str, Any], lead: dict[str, Any], task: dict[str, Any]) -> tuple[str, str]:
    subject = ""
    body = ""
    for line in text.splitlines():
        if line.strip().upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
            break
    if "BODY:" in text:
        body = text.split("BODY:", 1)[1].strip()
    if not subject or not body:
        fallback_subject, fallback_body = fallback_automation_message(profile, lead, task)
        subject = subject or fallback_subject
        body = body or fallback_body
    return subject, body


def generate_task_email_content(task_id: int) -> tuple[bool, str]:
    task = get_task_by_id(task_id)
    if not task:
        return False, "Task not found."
    if task.get("related_type") != "lead":
        return False, "Email generation currently supports lead follow-up tasks."
    lead = get_lead_by_id(int(task["related_id"]))
    if not lead:
        return False, "Lead not found."
    profile = get_company_profile()
    fallback_subject, fallback_body = fallback_automation_message(profile, lead, task)
    fallback = f"SUBJECT: {fallback_subject}\nBODY:\n{fallback_body}"
    generated = ai_generate(build_automation_task_prompt(profile, lead, task), fallback)
    subject, body = parse_generated_subject_body(generated, profile, lead, task)
    execute(
        """
        UPDATE tasks
        SET subject = ?, generated_content = ?, generated_at = ?, status = 'Generated', last_error = ''
        WHERE id = ? AND company_id = ?
        """,
        (subject, body, now_string(), task_id, ACTIVE_COMPANY_ID),
    )
    add_lead_event(int(task["related_id"]), "Automated Follow-Up Generated", f"{task.get('title', '')} content was generated.")
    return True, "Task content generated."


def send_task_email(task_id: int) -> tuple[bool, str]:
    task = get_task_by_id(task_id)
    if not task:
        return False, "Task not found."
    if task.get("related_type") != "lead":
        return False, "Only lead follow-up email tasks can be sent from this control."
    lead = get_lead_by_id(int(task["related_id"]))
    if not lead:
        return False, "Lead not found."
    if not lead.get("client_email"):
        return False, "This lead does not have a client email."
    if not task.get("subject") or not task.get("generated_content"):
        ok, message = generate_task_email_content(task_id)
        if not ok:
            return False, message
        task = get_task_by_id(task_id) or task
    profile = get_company_profile()
    ok, message = send_email_via_resend(lead.get("client_email", ""), task.get("subject", "Project follow-up"), task.get("generated_content", ""), profile)
    if ok:
        execute(
            """
            UPDATE tasks
            SET status = 'Sent', sent_at = ?, completed_at = ?, last_error = ''
            WHERE id = ? AND company_id = ?
            """,
            (now_string(), now_string(), task_id, ACTIVE_COMPANY_ID),
        )
        execute(
            "UPDATE leads SET last_contact_date = ?, next_followup_date = ? WHERE id = ? AND company_id = ?",
            (today_string(), future_date_string(2), lead["id"], ACTIVE_COMPANY_ID),
        )
        add_lead_event(int(task["related_id"]), "Automated Follow-Up Sent", f"{task.get('title', '')} sent to {lead.get('client_email', '')}.")
        return True, "Email sent."
    execute("UPDATE tasks SET last_error = ? WHERE id = ? AND company_id = ?", (message, task_id, ACTIVE_COMPANY_ID))
    add_lead_event(int(task["related_id"]), "Automated Follow-Up Send Failed", f"{task.get('title', '')} failed: {message}")
    return False, message


def send_all_due_followup_tasks() -> list[tuple[int, bool, str]]:
    tasks = fetch_all(
        """
        SELECT * FROM tasks
        WHERE company_id = ? AND category = 'proposal_followup'
          AND status IN ('Pending', 'Generated')
          AND due_date <= ?
        ORDER BY due_date ASC, id ASC
        """,
        (ACTIVE_COMPANY_ID, today_string()),
    )
    return [(int(task["id"]), *send_task_email(int(task["id"]))) for task in tasks]


# ============================================================
# Analytics
# ============================================================

def calculate_dashboard_metrics(leads: list[dict[str, Any]], projects: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    won = [lead for lead in leads if lead.get("status") == "Won"]
    lost = [lead for lead in leads if lead.get("status") == "Lost"]
    no_response = [lead for lead in leads if lead.get("status") == "No Response"]
    closed_count = len(won) + len(lost)
    win_rate = (len(won) / closed_count * 100) if closed_count else 0.0
    lead_overdue = [lead for lead in leads if get_lead_reminder_status(lead) == "Overdue"]
    lead_due = [lead for lead in leads if get_lead_reminder_status(lead) == "Due Today"]
    lead_upcoming = [lead for lead in leads if get_lead_reminder_status(lead) == "Upcoming"]
    task_summary = task_counts(tasks)
    active_projects = [project for project in projects if project.get("project_status") == "Active"]
    completed_projects = [project for project in projects if project.get("project_status") == "Completed"]

    return {
        "total_leads": len(leads),
        "won_count": len(won),
        "lost_count": len(lost),
        "no_response_count": len(no_response),
        "win_rate": win_rate,
        "total_quoted_value": sum(safe_float(lead.get("estimated_value")) for lead in leads),
        "total_won_value": sum(safe_float(lead.get("estimated_value")) for lead in won),
        "average_won_job_value": (sum(safe_float(lead.get("estimated_value")) for lead in won) / len(won)) if won else 0.0,
        "lead_overdue_count": len(lead_overdue),
        "lead_due_count": len(lead_due),
        "lead_upcoming_count": len(lead_upcoming),
        "lead_overdue": lead_overdue,
        "lead_due": lead_due,
        "lead_upcoming": lead_upcoming,
        "tasks_due_today": task_summary.get("Due Today", 0),
        "tasks_overdue": task_summary.get("Overdue", 0),
        "tasks_upcoming": task_summary.get("Upcoming", 0),
        "tasks_sent": task_summary.get("Sent", 0),
        "tasks_done": task_summary.get("Done", 0),
        "active_projects": len(active_projects),
        "completed_projects": len(completed_projects),
    }


def group_analytics(leads: list[dict[str, Any]], field: str) -> pd.DataFrame:
    groups = sorted(set((lead.get(field) or "Unknown") for lead in leads))
    rows = []
    for group in groups:
        subset = [lead for lead in leads if (lead.get(field) or "Unknown") == group]
        won = [lead for lead in subset if lead.get("status") == "Won"]
        lost = [lead for lead in subset if lead.get("status") == "Lost"]
        closed = len(won) + len(lost)
        rows.append(
            {
                "Group": group,
                "Leads": len(subset),
                "Won": len(won),
                "Lost": len(lost),
                "Win Rate %": round((len(won) / closed * 100) if closed else 0.0, 1),
                "Quoted Value": sum(safe_float(lead.get("estimated_value")) for lead in subset),
                "Won Value": sum(safe_float(lead.get("estimated_value")) for lead in won),
            }
        )
    return pd.DataFrame(rows)


# ============================================================
# UI Helpers + Styling
# ============================================================

def metric_card(label: str, value: Any, accent: str = "#d4af37", note: str = "") -> None:
    st.markdown(
        f"""
        <div class="bf-metric-card" style="--accent:{accent};">
            <div class="bf-metric-label">{label}</div>
            <div class="bf-metric-value">{value}</div>
            <div class="bf-metric-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def dark_bar_chart(df: pd.DataFrame, x_col: str, y_cols: str | list[str], title: str) -> None:
    if df is None or df.empty:
        st.info("No chart data yet.")
        return

    if isinstance(y_cols, str):
        y_cols = [y_cols]

    if len(y_cols) == 1:
        chart = (
            alt.Chart(df)
            .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
            .encode(
                x=alt.X(f"{x_col}:N", title=None, axis=alt.Axis(labelColor="#e7e7e7", labelAngle=-25)),
                y=alt.Y(f"{y_cols[0]}:Q", title=None, axis=alt.Axis(labelColor="#e7e7e7", gridColor="rgba(255,255,255,0.08)")),
                color=alt.value("#d4af37"),
                tooltip=list(df.columns),
            )
            .properties(height=260, title=title)
        )
    else:
        melted = df.melt(id_vars=[x_col], value_vars=y_cols, var_name="Metric", value_name="Value")
        chart = (
            alt.Chart(melted)
            .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
            .encode(
                x=alt.X(f"{x_col}:N", title=None, axis=alt.Axis(labelColor="#e7e7e7", labelAngle=-25)),
                y=alt.Y("Value:Q", title=None, axis=alt.Axis(labelColor="#e7e7e7", gridColor="rgba(255,255,255,0.08)")),
                xOffset="Metric:N",
                color=alt.Color("Metric:N", scale=alt.Scale(range=["#d4af37", "#35c46a", "#4ea1ff", "#ffcc66"]), legend=alt.Legend(labelColor="#e7e7e7", titleColor="#f2d675")),
                tooltip=[x_col, "Metric", "Value"],
            )
            .properties(height=280, title=title)
        )

    chart = (
        chart.configure_view(fill="#111113", stroke="rgba(212,175,55,0.22)")
        .configure_axis(domainColor="rgba(255,255,255,0.18)", tickColor="rgba(255,255,255,0.18)", gridColor="rgba(255,255,255,0.06)")
        .configure_title(color="#ffffff", fontSize=16, fontWeight="bold", anchor="start")
        .configure(background="#111113")
    )

    st.markdown("<div class='bf-chart-card'>", unsafe_allow_html=True)
    st.altair_chart(chart, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


def select_existing_lead(leads: list[dict[str, Any]], key_prefix: str) -> dict[str, Any] | None:
    if not leads:
        return None
    options = {f"{lead['id']} — {lead.get('client_name', '')} — {lead.get('project_type', '')}": int(lead["id"]) for lead in leads}
    selected_label = st.selectbox("Choose saved lead", list(options.keys()), key=f"{key_prefix}_saved_lead")
    return get_lead_by_id(options[selected_label])


def select_existing_project(projects: list[dict[str, Any]], key_prefix: str) -> dict[str, Any] | None:
    if not projects:
        return None
    options = {f"{project['id']} — {project.get('client_name', '')} — {project.get('current_stage', '')}": int(project["id"]) for project in projects}
    selected_label = st.selectbox("Choose project", list(options.keys()), key=f"{key_prefix}_saved_project")
    return get_project_by_id(options[selected_label])


# ============================================================
# Initialize App Data
# ============================================================

init_database()
ensure_schema_migrations()

active_user = require_auth_if_enabled()
ACTIVE_OWNER_KEY = active_user["owner_key"]
ACTIVE_USER_LABEL = active_user["label"]
ACTIVE_COMPANY_ID = ensure_company_for_owner(ACTIVE_OWNER_KEY, ACTIVE_USER_LABEL)

# Legacy JSON migration is only useful for the local testing build.
if not USE_POSTGRES:
    migrate_legacy_json_if_needed()

profile = get_company_profile()
leads = get_leads()
projects = get_projects()
tasks = get_tasks()
metrics = calculate_dashboard_metrics(leads, projects, tasks)


# ============================================================
# CSS Theme
# ============================================================

st.markdown(
    """
    <style>
    :root {
        --bf-black: #0b0b0c;
        --bf-charcoal: #151517;
        --bf-card: #1c1c1f;
        --bf-gold: #d4af37;
        --bf-soft-gold: #f2d675;
        --bf-text: #f7f7f7;
        --bf-muted: #e6e6e6;
    }

    .stApp {
        background: radial-gradient(circle at top left, rgba(212,175,55,0.10), transparent 28%), linear-gradient(135deg, #050505 0%, #101011 48%, #191919 100%);
        color: var(--bf-text);
    }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #070707 0%, #151515 100%);
        border-right: 1px solid rgba(212, 175, 55, 0.25);
    }
    section[data-testid="stSidebar"] * { color: #f5f5f5; }

    .bf-topbar {
        display: flex; align-items: center; justify-content: space-between;
        padding: 18px 22px; border: 1px solid rgba(212,175,55,0.25);
        background: rgba(16,16,18,0.92); border-radius: 18px; margin-bottom: 22px;
        box-shadow: 0 12px 30px rgba(0,0,0,0.35);
    }
    .bf-brand-title { font-size: 30px; font-weight: 800; letter-spacing: -0.03em; color: #fff; }
    .bf-brand-subtitle { color: #e7e7e7; font-size: 14px; margin-top: 3px; }
    .bf-gold { color: var(--bf-gold); }
    .bf-account-pill {
        border: 1px solid rgba(212,175,55,0.6); background: linear-gradient(135deg, rgba(212,175,55,0.18), rgba(212,175,55,0.05));
        color: #fff; padding: 10px 14px; border-radius: 999px; font-size: 14px; white-space: nowrap;
    }
    .bf-card {
        border: 1px solid rgba(212,175,55,0.18); background: linear-gradient(135deg, rgba(28,28,31,0.96), rgba(18,18,20,0.94));
        border-radius: 18px; padding: 18px; margin-bottom: 16px; box-shadow: 0 10px 28px rgba(0,0,0,0.28);
    }
    .bf-section-label { color: var(--bf-gold); font-size: 13px; text-transform: uppercase; letter-spacing: .12em; font-weight: 700; margin-bottom: 4px; }
    .bf-metric-card {
        border: 1px solid rgba(212,175,55,0.16); background: linear-gradient(135deg, rgba(30,30,34,.98), rgba(15,15,17,.98));
        border-radius: 18px; padding: 16px 16px 14px; min-height: 112px; box-shadow: 0 12px 28px rgba(0,0,0,.30); position: relative; overflow: hidden;
    }
    .bf-metric-card:before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--accent); box-shadow:0 0 18px var(--accent); }
    .bf-metric-label { color:#fff; font-size:13px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; margin-bottom:8px; }
    .bf-metric-value { color:var(--accent); font-size:30px; line-height:1.1; font-weight:900; letter-spacing:-.04em; }
    .bf-metric-note { color:#f1f1f1; font-size:12px; margin-top:6px; }
    .bf-chart-card { border:1px solid rgba(212,175,55,.16); background:linear-gradient(135deg, rgba(22,22,25,.98), rgba(10,10,12,.98)); border-radius:18px; padding:16px; margin-bottom:18px; box-shadow:0 12px 28px rgba(0,0,0,.32); }

    .stButton>button, .stDownloadButton>button, div[data-testid="stFormSubmitButton"] button {
        border-radius:999px!important; border:1px solid rgba(212,175,55,.85)!important;
        background:linear-gradient(135deg,#f2d675,#d4af37 48%,#a98216)!important;
        color:#050505!important; font-weight:900!important; box-shadow:0 8px 18px rgba(212,175,55,.22)!important;
    }
    .stButton>button *, .stDownloadButton>button *, div[data-testid="stFormSubmitButton"] button *, .stButton>button p, .stDownloadButton>button p, div[data-testid="stFormSubmitButton"] button p { color:#050505!important; font-weight:900!important; }
    .stButton>button:hover, .stDownloadButton>button:hover, div[data-testid="stFormSubmitButton"] button:hover { border:1px solid #fff0a8!important; filter:brightness(1.08); }

    p, li, label, .stMarkdown, .stCaption, div[data-testid="stMarkdownContainer"] { color:#f5f5f5; }
    h1, h2, h3 { color:#fff; }
    .stDataFrame, div[data-testid="stDataFrame"] { border-radius:16px!important; overflow:hidden!important; }
    div[data-testid="stAlert"] { border-radius:14px; }
    .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] { border-radius:12px!important; }
    .stSelectbox div[data-baseweb="select"] > div { background-color:#f7f1d1!important; color:#050505!important; border-radius:12px!important; }
    .stSelectbox div[data-baseweb="select"] span, .stSelectbox div[data-baseweb="select"] input, .stSelectbox div[data-baseweb="select"] div { color:#050505!important; }
    .stSelectbox svg { fill:#050505!important; }
    div[data-baseweb="popover"] ul, div[data-baseweb="menu"] { background-color:#111113!important; color:#fff!important; }
    div[data-baseweb="popover"] li, div[data-baseweb="menu"] li { color:#fff!important; }
    </style>
    """,
    unsafe_allow_html=True,
)

account_name = profile.get("company_name") or "Account"
st.markdown(
    f"""
    <div class="bf-topbar">
        <div>
            <div class="bf-brand-title">BuilderFlow</div>
            <div class="bf-brand-subtitle">Lead conversion, proposal automation, project tracking, and task control for remodelers and builders.</div>
        </div>
        <div class="bf-account-pill">👤 {account_name}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if has_openai_key():
    st.success("AI mode is active. Your OpenAI API key was detected.")
else:
    st.warning("Template mode is active. Add OPENAI_API_KEY to your .env file to use AI generation.")

if email_sending_ready(profile):
    st.success("Real email sending is configured.")
else:
    st.info("Real email sending is not fully configured yet. Add Resend setup in Account plus RESEND_API_KEY in .env locally or Cloud Secrets when deployed.")

if USE_POSTGRES:
    st.info("Cloud database mode is active. BuilderFlow is using DATABASE_URL for hosted Postgres storage.")
else:
    st.info("Local testing mode is active. BuilderFlow is using builderflow.db on your computer.")


# ============================================================
# Sidebar Navigation
# ============================================================

with st.sidebar:
    st.markdown("### 🏗️ BuilderFlow")
    st.markdown("<span style='color:#d4af37;font-size:13px;font-weight:700;'>NAVIGATION</span>", unsafe_allow_html=True)
    main_area = st.radio(
        "Main Menu",
        ["Owner Dashboard", "Leads", "Projects", "Task Center", "Automations", "Growth Insights", "Client Update", "Referral / Review", "Feedback / Report Bug", "Saved Outputs", "Account"],
        label_visibility="collapsed",
    )
    lead_page = None
    project_page = None
    if main_area == "Leads":
        lead_page = st.selectbox("Leads", ["Lead Workspace", "Follow-Up Sequences", "Lead Follow-Up", "Proposal Draft"])
    if main_area == "Projects":
        project_page = st.selectbox("Projects", ["Project Pipeline", "Project Details"])
    st.divider()
    st.caption(f"Workspace: {account_name}")
    st.caption(f"Storage: {storage_mode_label()}")
    st.caption(f"Access: {authentication_mode_label()}")
    if AUTH_REQUIRED:
        st.caption(f"Signed in: {ACTIVE_USER_LABEL}")
        if st.button("Log out"):
            st.logout()

if main_area == "Leads":
    current_page = lead_page
elif main_area == "Projects":
    current_page = project_page
else:
    current_page = main_area


# ============================================================
# Page: Account
# ============================================================

if current_page == "Account":
    st.header("Account")
    st.markdown("<div class='bf-card'><div class='bf-section-label'>Workspace Setup</div>BuilderFlow now supports local testing mode and hosted beta mode. Company data is scoped to the active workspace, and login-gated workspaces activate when authentication is enabled.</div>", unsafe_allow_html=True)

    with st.form("company_profile_form"):
        company_name = st.text_input("Company Name", value=profile.get("company_name", ""), placeholder="Example: Summit Renovations")
        contact_name = st.text_input("Owner / Salesperson Name", value=profile.get("contact_name", ""), placeholder="Example: Mike")
        c1, c2 = st.columns(2)
        phone = c1.text_input("Phone Number", value=profile.get("phone", ""))
        email = c2.text_input("Business Contact Email", value=profile.get("email", ""))
        website = st.text_input("Website", value=profile.get("website", ""))
        service_area = st.text_input("Service Area", value=profile.get("service_area", ""))
        main_services = st.text_area("Main Services", value=profile.get("main_services", ""))
        c3, c4 = st.columns(2)
        booking_link = c3.text_input("Booking Link", value=profile.get("booking_link", ""))
        review_link = c4.text_input("Review Link", value=profile.get("review_link", ""))
        tone_options = ["Professional", "Friendly", "Premium", "Simple", "Confident", "Warm"]
        current_tone = profile.get("preferred_tone", "Professional")
        tone_index = tone_options.index(current_tone) if current_tone in tone_options else 0
        preferred_tone = st.selectbox("Preferred Writing Tone", tone_options, index=tone_index)
        st.divider()
        st.subheader("Real Email Sending Setup")
        c5, c6 = st.columns(2)
        sender_name = c5.text_input("Sender Display Name", value=profile.get("sender_name", ""), placeholder="Example: Mike at Summit Renovations")
        sender_email = c6.text_input("Sender Email / From Address", value=profile.get("sender_email", ""), placeholder="Example: onboarding@resend.dev")
        saved = st.form_submit_button("Save Company Profile")

    if saved:
        save_company_profile(
            {
                "company_name": company_name,
                "contact_name": contact_name,
                "phone": phone,
                "email": email,
                "website": website,
                "service_area": service_area,
                "main_services": main_services,
                "booking_link": booking_link,
                "review_link": review_link,
                "preferred_tone": preferred_tone,
                "sender_name": sender_name,
                "sender_email": sender_email,
            }
        )
        st.success("Company profile saved.")
        st.rerun()

    st.divider()
    st.subheader("Signature Preview")
    st.code(get_company_signature(get_company_profile()))

    st.divider()
    st.subheader("Beta Deployment Readiness")
    readiness_cols = st.columns(3)
    with readiness_cols[0]:
        metric_card("Storage", storage_mode_label(), "#4ea1ff", "Local testing or hosted data")
    with readiness_cols[1]:
        metric_card("Access", authentication_mode_label(), "#d4af37", "Workspace separation mode")
    with readiness_cols[2]:
        metric_card("Email", "Ready" if email_sending_ready(get_company_profile()) else "Needs Setup", "#35c46a" if email_sending_ready(get_company_profile()) else "#ffcc66", "Resend + sender email")


# ============================================================
# Page: Owner Dashboard
# ============================================================

if current_page == "Owner Dashboard":
    st.header("Owner Dashboard")
    if profile.get("company_name"):
        st.subheader(profile.get("company_name"))
        st.caption(f"Service area: {profile.get('service_area', '')}")

    render_onboarding_checklist(profile, leads, projects, tasks)

    row1 = st.columns(4)
    with row1[0]: metric_card("Total Leads", metrics["total_leads"], "#4ea1ff", "Total opportunities")
    with row1[1]: metric_card("Jobs Won", metrics["won_count"], "#35c46a", "Converted leads")
    with row1[2]: metric_card("Active Projects", metrics["active_projects"], "#d4af37", "Post-sale work")
    with row1[3]: metric_card("Win Rate", f"{metrics['win_rate']:.1f}%", "#f2d675", "Won / closed leads")

    row2 = st.columns(4)
    with row2[0]: metric_card("Lead Overdue", metrics["lead_overdue_count"], "#ff5c5c", "Manual follow-up risk")
    with row2[1]: metric_card("Tasks Due", metrics["tasks_due_today"], "#ffcc66", "Action today")
    with row2[2]: metric_card("Tasks Overdue", metrics["tasks_overdue"], "#ff9f43", "Needs attention")
    with row2[3]: metric_card("Completed Projects", metrics["completed_projects"], "#35c46a", "Finished work")

    st.divider()
    row3 = st.columns(3)
    with row3[0]: metric_card("Total Quoted Value", f"${metrics['total_quoted_value']:,.0f}", "#d4af37", "Pipeline value")
    with row3[1]: metric_card("Total Won Value", f"${metrics['total_won_value']:,.0f}", "#35c46a", "Converted revenue")
    with row3[2]: metric_card("Average Won Job", f"${metrics['average_won_job_value']:,.0f}", "#f2d675", "Average win")

    st.divider()
    st.subheader("Lead Follow-Up Action Board")
    board_cols = st.columns(3)
    with board_cols[0]:
        st.markdown("### 🔴 Overdue")
        if not metrics["lead_overdue"]: st.write("None")
        for lead in metrics["lead_overdue"]:
            st.warning(f"{lead.get('client_name')} — {lead.get('project_type')}\n\nDue: {lead.get('next_followup_date')}")
    with board_cols[1]:
        st.markdown("### 🟡 Due Today")
        if not metrics["lead_due"]: st.write("None")
        for lead in metrics["lead_due"]:
            st.info(f"{lead.get('client_name')} — {lead.get('project_type')}\n\nDue: {lead.get('next_followup_date')}")
    with board_cols[2]:
        st.markdown("### 🟢 Upcoming")
        if not metrics["lead_upcoming"]: st.write("None")
        for lead in metrics["lead_upcoming"]:
            st.success(f"{lead.get('client_name')} — {lead.get('project_type')}\n\nDue: {lead.get('next_followup_date')}")

    st.divider()
    task_summary = task_counts(tasks)
    st.subheader("Task Center Snapshot")
    task_cols = st.columns(4)
    with task_cols[0]: metric_card("Overdue Tasks", task_summary.get("Overdue", 0), "#ff5c5c", "Unified task center")
    with task_cols[1]: metric_card("Due Today", task_summary.get("Due Today", 0), "#ffcc66", "Ready to act")
    with task_cols[2]: metric_card("Upcoming", task_summary.get("Upcoming", 0), "#4ea1ff", "Next 7 days")
    with task_cols[3]: metric_card("Sent / Done", task_summary.get("Sent", 0) + task_summary.get("Done", 0), "#35c46a", "Completed actions")

    st.divider()
    df = leads_to_dataframe(leads)
    if df.empty:
        st.info("No leads added yet.")
    else:
        status_counts = df["Status"].value_counts().reset_index()
        status_counts.columns = ["Status", "Count"]
        dark_bar_chart(status_counts, "Status", "Count", "Pipeline by Status")
        st.subheader("Lead Pipeline Table")
        st.dataframe(df, use_container_width=True)

    st.divider()
    st.subheader("AI Owner Insights")
    summary_text = f"""
Total leads: {metrics['total_leads']}
Won jobs: {metrics['won_count']}
Lost jobs: {metrics['lost_count']}
Active projects: {metrics['active_projects']}
Completed projects: {metrics['completed_projects']}
Tasks due today: {metrics['tasks_due_today']}
Tasks overdue: {metrics['tasks_overdue']}
Win rate: {metrics['win_rate']:.1f}%
Quoted value: ${metrics['total_quoted_value']:,.0f}
Won value: ${metrics['total_won_value']:,.0f}
"""
    if st.button("Generate Owner Insights"):
        insight = ai_generate(build_owner_insight_prompt(profile, summary_text), fallback_owner_insights(metrics))
        add_output("Owner Insights", "Business Dashboard", insight)
        st.session_state["owner_insights"] = insight
    if st.session_state.get("owner_insights"):
        st.text_area("Owner insights", st.session_state["owner_insights"], height=320)


# ============================================================
# Page: Lead Workspace
# ============================================================

PROJECT_TYPE_OPTIONS = ["Kitchen Renovation", "Bathroom Renovation", "Basement Development", "Whole-Home Renovation", "Addition", "Custom Home", "Exterior Renovation", "Other"]
LEAD_SOURCE_OPTIONS = ["Google", "Referral", "Website", "Facebook", "Instagram", "Repeat Client", "Marketplace", "Yard Sign", "Networking", "Other", "Unknown"]
CONTACT_METHOD_OPTIONS = ["Email", "Phone", "Text", "No Preference"]
STATUS_OPTIONS = ["New", "Contacted", "Follow-Up Sent", "Proposal Sent", "Won", "Lost", "No Response"]
PRIORITY_OPTIONS = ["Low", "Medium", "High"]

if current_page == "Lead Workspace":
    st.header("Leads")
    st.markdown(
        "<div class='bf-card'><div class='bf-section-label'>Lead Workspace</div>"
        "Add new leads, review your full pipeline, and manage one selected lead from a single place. "
        "This replaces the old separate Lead Tracker and Lead Details pages."
        "</div>",
        unsafe_allow_html=True
    )

    leads = get_leads()
    add_tab, pipeline_tab, selected_tab = st.tabs(["Add New Lead", "All Leads", "Selected Lead Workspace"])

    # -----------------------------
    # Add New Lead
    # -----------------------------
    with add_tab:
        st.subheader("Add New Lead")
        with st.form("add_lead_form"):
            client_name = st.text_input("Client Name")
            c1, c2 = st.columns(2)
            project_type = c1.selectbox("Project Type", PROJECT_TYPE_OPTIONS)
            lead_source = c2.selectbox("Lead Source", LEAD_SOURCE_OPTIONS)

            c3, c4 = st.columns(2)
            client_email = c3.text_input("Client Email")
            client_phone = c4.text_input("Client Phone")

            c5, c6 = st.columns(2)
            preferred_contact_method = c5.selectbox("Preferred Contact Method", CONTACT_METHOD_OPTIONS)
            project_address = c6.text_input("Project Address / Location")

            c7, c8 = st.columns(2)
            budget = c7.text_input("Budget Range", placeholder="$40,000 - $60,000")
            timeline = c8.text_input("Timeline", placeholder="Wants to start in 2 months")

            estimated_value = st.text_input("Estimated Job Value", placeholder="50000")
            notes = st.text_area("Lead Notes")

            c9, c10 = st.columns(2)
            last_contact_date = c9.date_input("Last Contact Date", value=date.today())
            next_followup_date = c10.date_input("Next Follow-Up Date", value=date.today() + timedelta(days=2))

            followup_priority = st.selectbox("Follow-Up Priority", PRIORITY_OPTIONS, index=1)
            followup_notes = st.text_area("Follow-Up Notes")
            submitted = st.form_submit_button("Save Lead")

        if submitted:
            if not client_name.strip():
                st.error("Client name is required.")
            else:
                add_lead(
                    client_name, project_type, lead_source, client_email, client_phone,
                    preferred_contact_method, project_address, budget, timeline,
                    estimated_value, notes, str(last_contact_date), str(next_followup_date),
                    followup_priority, followup_notes
                )
                st.success(f"Lead saved for {client_name}.")
                st.rerun()

    # -----------------------------
    # All Leads
    # -----------------------------
    with pipeline_tab:
        st.subheader("Lead Pipeline")
        if not leads:
            st.info("No leads yet.")
        else:
            lead_df = leads_to_dataframe(leads)
            st.dataframe(lead_df, use_container_width=True)
            csv = lead_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download Leads as CSV",
                data=csv,
                file_name="builderflow_leads.csv",
                mime="text/csv"
            )

    # -----------------------------
    # Selected Lead Workspace
    # -----------------------------
    with selected_tab:
        st.subheader("Selected Lead Workspace")

        if not leads:
            st.info("Add a lead first.")
        else:
            lead = select_existing_lead(leads, "lead_workspace")
            if lead:
                lead_id = int(lead["id"])

                top = st.columns(4)
                top[0].metric("Status", lead.get("status", "New"))
                top[1].metric("Value", f"${safe_float(lead.get('estimated_value')):,.0f}")
                top[2].metric("Reminder", get_lead_reminder_status(lead))
                top[3].metric("Source", lead.get("lead_source", "Unknown"))

                st.divider()
                left, right = st.columns(2)

                with left:
                    st.subheader("Client / Project")
                    st.write(f"**Client:** {lead.get('client_name', '')}")
                    st.write(f"**Email:** {lead.get('client_email', '') or 'Not provided'}")
                    st.write(f"**Phone:** {lead.get('client_phone', '') or 'Not provided'}")
                    st.write(f"**Address:** {lead.get('project_address', '') or 'Not provided'}")
                    st.write(f"**Type:** {lead.get('project_type', '')}")
                    st.write(f"**Budget:** {lead.get('budget', '')}")
                    st.write(f"**Timeline:** {lead.get('timeline', '')}")

                with right:
                    st.subheader("Follow-Up")
                    st.write(f"**Last Contact:** {lead.get('last_contact_date', '')}")
                    st.write(f"**Next Follow-Up:** {lead.get('next_followup_date', '')}")
                    st.write(f"**Priority:** {lead.get('followup_priority', '')}")
                    st.write(f"**Notes:** {lead.get('followup_notes', '') or 'None'}")
                    st.write(f"**Closed:** {lead.get('closed_at', '') or 'Not closed'}")
                    st.write(f"**Status Notes:** {lead.get('reason_lost', '') or 'None'}")

                detail_tab, status_tab, activity_tab, automation_tab, output_tab = st.tabs(
                    ["Edit Details", "Update / Close Lead", "Activity & Timeline", "Proposal Automation", "Saved Outputs"]
                )

                # -----------------------------
                # Edit Details
                # -----------------------------
                with detail_tab:
                    st.subheader("Edit Lead Details")
                    with st.form(f"edit_lead_{lead_id}"):
                        edit_name = st.text_input("Client Name", value=lead.get("client_name", ""))
                        e1, e2 = st.columns(2)
                        edit_type = e1.selectbox(
                            "Project Type",
                            PROJECT_TYPE_OPTIONS,
                            index=PROJECT_TYPE_OPTIONS.index(lead.get("project_type")) if lead.get("project_type") in PROJECT_TYPE_OPTIONS else len(PROJECT_TYPE_OPTIONS)-1
                        )
                        edit_source = e2.selectbox(
                            "Lead Source",
                            LEAD_SOURCE_OPTIONS,
                            index=LEAD_SOURCE_OPTIONS.index(lead.get("lead_source")) if lead.get("lead_source") in LEAD_SOURCE_OPTIONS else len(LEAD_SOURCE_OPTIONS)-1
                        )

                        e3, e4 = st.columns(2)
                        edit_email = e3.text_input("Client Email", value=lead.get("client_email", ""))
                        edit_phone = e4.text_input("Client Phone", value=lead.get("client_phone", ""))

                        e5, e6 = st.columns(2)
                        edit_contact = e5.selectbox(
                            "Preferred Contact",
                            CONTACT_METHOD_OPTIONS,
                            index=CONTACT_METHOD_OPTIONS.index(lead.get("preferred_contact_method")) if lead.get("preferred_contact_method") in CONTACT_METHOD_OPTIONS else 0
                        )
                        edit_address = e6.text_input("Project Address", value=lead.get("project_address", ""))

                        e7, e8 = st.columns(2)
                        edit_budget = e7.text_input("Budget", value=lead.get("budget", ""))
                        edit_timeline = e8.text_input("Timeline", value=lead.get("timeline", ""))

                        edit_value = st.text_input("Estimated Job Value", value=lead.get("estimated_value", ""))
                        edit_notes = st.text_area("Lead Notes", value=lead.get("notes", ""))
                        edit_submit = st.form_submit_button("Save Lead Changes")

                    if edit_submit:
                        if not edit_name.strip():
                            st.error("Client name cannot be blank.")
                        else:
                            edit_lead_details(
                                lead_id, edit_name, edit_type, edit_source, edit_email, edit_phone,
                                edit_contact, edit_address, edit_budget, edit_timeline, edit_value, edit_notes
                            )
                            st.success("Lead details saved.")
                            st.rerun()

                # -----------------------------
                # Update / Close Lead
                # -----------------------------
                with status_tab:
                    st.subheader("Update Lead Status / Reminder")
                    current_status = lead.get("status", "New")
                    status_index = STATUS_OPTIONS.index(current_status) if current_status in STATUS_OPTIONS else 0

                    with st.form(f"status_update_{lead_id}"):
                        status = st.selectbox("New Status", STATUS_OPTIONS, index=status_index)

                        d1, d2 = st.columns(2)
                        last_contact_update = d1.date_input(
                            "Last Contact Date",
                            value=parse_iso_date(lead.get("last_contact_date")),
                            key=f"workspace_last_contact_{lead_id}"
                        )
                        next_followup_update = d2.date_input(
                            "Next Follow-Up Date",
                            value=parse_iso_date(lead.get("next_followup_date")),
                            key=f"workspace_next_followup_{lead_id}"
                        )

                        current_priority = lead.get("followup_priority", "Medium")
                        priority_index = PRIORITY_OPTIONS.index(current_priority) if current_priority in PRIORITY_OPTIONS else 1
                        priority = st.selectbox("Follow-Up Priority", PRIORITY_OPTIONS, index=priority_index)

                        reason_lost = st.text_area("Reason Lost / Status Notes", value=lead.get("reason_lost", ""))
                        followup_notes_update = st.text_area("Follow-Up Notes", value=lead.get("followup_notes", ""))
                        update_submit = st.form_submit_button("Update Lead")

                    if update_submit:
                        update_lead_status(
                            lead_id, status, reason_lost, str(last_contact_update),
                            str(next_followup_update), priority, followup_notes_update
                        )
                        st.success("Lead updated.")
                        st.rerun()

                    st.divider()
                    st.subheader("Finish / Close This Lead")
                    with st.form(f"close_lead_{lead_id}"):
                        close_status = st.selectbox("Close Status", ["Won", "Lost", "No Response"])
                        close_note = st.text_area("Closing Note / Reason")
                        close_submit = st.form_submit_button("Finish / Close Lead")
                    if close_submit:
                        close_lead_from_details(lead_id, close_status, close_note)
                        st.success(f"Lead marked as {close_status}.")
                        st.rerun()

                    st.divider()
                    st.subheader("Reopen Lead")
                    reopen_note = st.text_area("Reopen Note", key=f"reopen_note_{lead_id}")
                    if st.button("Reopen Lead", key=f"reopen_button_{lead_id}"):
                        reopen_lead(lead_id, reopen_note)
                        st.success("Lead reopened.")
                        st.rerun()

                    st.divider()
                    st.subheader("Delete Lead")
                    st.warning("Delete only test data or mistakes. This removes linked lead records.")
                    confirm_delete = st.checkbox("I understand this will permanently remove this lead.", key=f"delete_confirm_{lead_id}")
                    if st.button("Delete Lead", key=f"delete_button_{lead_id}"):
                        if not confirm_delete:
                            st.error("Check the confirmation box first.")
                        else:
                            delete_lead(lead_id)
                            st.success("Lead deleted.")
                            st.rerun()

                # -----------------------------
                # Activity & Timeline
                # -----------------------------
                with activity_tab:
                    st.subheader("Add Activity Note")
                    with st.form(f"activity_note_{lead_id}"):
                        event_type = st.selectbox(
                            "Activity Type",
                            ["Call", "Email", "Meeting", "Site Visit", "Client Concern", "Quote Update", "Internal Note", "Follow-Up", "Other"]
                        )
                        activity_note = st.text_area("Activity Note")
                        activity_submit = st.form_submit_button("Save Activity Note")

                    if activity_submit:
                        if not activity_note.strip():
                            st.error("Add a note before saving.")
                        else:
                            add_lead_event(lead_id, event_type, activity_note.strip())
                            st.success("Activity note saved.")
                            st.rerun()

                    st.divider()
                    st.subheader("Lead Timeline")
                    timeline_items = []
                    for event in get_lead_events(lead_id):
                        timeline_items.append((event.get("created_at", ""), event.get("event_type", ""), event.get("note", "")))
                    for task in get_tasks_for_lead(lead_id):
                        if task.get("sent_at"):
                            timeline_items.append((task.get("sent_at", ""), f"Task Sent — {task.get('title', '')}", task.get("subject", "")))
                    timeline_items.sort(key=lambda item: item[0], reverse=True)

                    if not timeline_items:
                        st.info("No timeline activity yet.")
                    else:
                        for created_at, event_type, note in timeline_items:
                            st.markdown(f"**{event_type}** — {created_at}")
                            st.write(note)
                            st.divider()

                # -----------------------------
                # Proposal Automation
                # -----------------------------
                with automation_tab:
                    st.subheader("Proposal Automation & Version History")
                    lead_tasks = get_tasks_for_lead(lead_id)
                    proposal_tasks = [task for task in lead_tasks if task.get("category") == "proposal_followup"]

                    if not proposal_tasks:
                        st.info("No scheduled proposal follow-ups yet. Generate a proposal for this saved lead to create the 5-touch plan.")
                    else:
                        task_df = pd.DataFrame([
                            {
                                "Task": task.get("title", ""),
                                "Due": task.get("due_date", ""),
                                "Status": task.get("status", ""),
                                "Subject": task.get("subject", "")
                            }
                            for task in proposal_tasks
                        ])
                        st.dataframe(task_df, use_container_width=True)

                    st.divider()
                    st.subheader("Proposal Version History")
                    proposals = get_proposal_versions(lead_id)
                    if not proposals:
                        st.info("No proposal versions saved yet.")
                    else:
                        for proposal in proposals:
                            with st.expander(f"Version {proposal.get('version_number')} — {proposal.get('created_at')}"):
                                st.text_area(
                                    "Proposal Content",
                                    proposal.get("content", ""),
                                    height=260,
                                    key=f"proposal_version_{proposal.get('id')}"
                                )

                # -----------------------------
                # Saved Outputs
                # -----------------------------
                with output_tab:
                    st.subheader("Saved Outputs for This Lead")
                    outputs = get_outputs_for_lead(lead_id, lead.get("client_name", ""))
                    if not outputs:
                        st.info("No saved outputs for this lead yet.")
                    else:
                        for output in outputs:
                            with st.expander(f"{output.get('output_type')} — {output.get('created_at')}"):
                                st.text_area(
                                    "Saved Content",
                                    output.get("content", ""),
                                    height=260,
                                    key=f"lead_output_{output.get('id')}"
                                )


# ============================================================
# Page: Follow-Up Sequences
# ============================================================

if current_page == "Follow-Up Sequences":
    st.header("Follow-Up Sequences")
    st.markdown("<div class='bf-card'><div class='bf-section-label'>Sales System</div>These follow-up systems are the structured sales logic behind BuilderFlow.</div>", unsafe_allow_html=True)
    for sequence in FOLLOWUP_SEQUENCE_OPTIONS:
        with st.expander(sequence):
            st.write(followup_sequence_instruction(sequence))


# ============================================================
# Page: Lead Follow-Up
# ============================================================

if current_page == "Lead Follow-Up":
    st.header("Lead Follow-Up")
    leads = get_leads()
    selected_lead = None
    if leads:
        use_saved = st.checkbox("Use an existing saved lead")
        if use_saved:
            selected_lead = select_existing_lead(leads, "manual_followup")
    key_suffix = selected_lead.get("id", "manual") if selected_lead else "manual"
    with st.form(f"followup_form_{key_suffix}"):
        sequence = st.selectbox("Follow-Up Sequence", FOLLOWUP_SEQUENCE_OPTIONS)
        client_name = st.text_input("Client Name", value=selected_lead.get("client_name", "") if selected_lead else "")
        client_email = st.text_input("Client Email", value=selected_lead.get("client_email", "") if selected_lead else "")
        project_type = st.text_input("Project Type", value=selected_lead.get("project_type", "") if selected_lead else "")
        budget = st.text_input("Budget", value=selected_lead.get("budget", "") if selected_lead else "")
        timeline = st.text_input("Timeline", value=selected_lead.get("timeline", "") if selected_lead else "")
        notes = st.text_area("Lead Notes", value=selected_lead.get("notes", "") if selected_lead else "")
        submitted = st.form_submit_button("Generate Follow-Up")
    if submitted:
        if not client_name or not project_type:
            st.error("Client name and project type are required.")
        else:
            result = ai_generate(build_follow_up_prompt(profile, client_name, project_type, budget, timeline, notes, sequence), fallback_follow_up(profile, client_name, project_type, budget, timeline, notes, sequence))
            lead_id = int(selected_lead["id"]) if selected_lead else None
            add_output(f"Follow-Up: {sequence}", client_name, result, lead_id=lead_id)
            if lead_id:
                mark_followup_generated(lead_id, sequence)
            st.session_state["last_manual_followup"] = {"client_email": client_email, "subject": f"Following up on your {project_type} project", "body": result}
    if st.session_state.get("last_manual_followup"):
        result = st.session_state["last_manual_followup"]
        st.text_area("Generated Follow-Up", result["body"], height=320)
        if result.get("client_email") and st.button("Send This Follow-Up Email Now"):
            ok, message = send_email_via_resend(result["client_email"], result["subject"], result["body"], profile)
            if ok:
                st.success("Email sent.")
            else:
                st.error(f"Email send failed: {message}")


# ============================================================
# Page: Proposal Draft
# ============================================================

if current_page == "Proposal Draft":
    st.header("Proposal Draft")
    leads = get_leads()
    selected_lead = None
    if leads:
        use_saved = st.checkbox("Use an existing saved lead", key="proposal_saved_checkbox")
        if use_saved:
            selected_lead = select_existing_lead(leads, "proposal")
    key_suffix = selected_lead.get("id", "manual") if selected_lead else "manual"
    with st.form(f"proposal_form_{key_suffix}"):
        client_name = st.text_input("Client Name", value=selected_lead.get("client_name", "") if selected_lead else "")
        project_type = st.text_input("Project Type", value=selected_lead.get("project_type", "") if selected_lead else "")
        budget = st.text_input("Budget", value=selected_lead.get("budget", "") if selected_lead else "")
        timeline = st.text_input("Timeline", value=selected_lead.get("timeline", "") if selected_lead else "")
        notes = st.text_area("Walkthrough / Meeting Notes", value=selected_lead.get("notes", "") if selected_lead else "")
        submitted = st.form_submit_button("Generate Proposal Draft")
    if submitted:
        if not client_name or not project_type:
            st.error("Client name and project type are required.")
        else:
            result = ai_generate(build_proposal_prompt(profile, client_name, project_type, budget, timeline, notes), fallback_proposal(profile, client_name, project_type, budget, timeline, notes))
            lead_id = int(selected_lead["id"]) if selected_lead else None
            add_output("Proposal Draft", client_name, result, lead_id=lead_id)
            if lead_id:
                mark_proposal_generated(lead_id)
                add_proposal_version(lead_id, client_name, project_type, result)
                created, message = create_proposal_followup_tasks(lead_id)
                if created:
                    st.success(message)
                else:
                    st.info(message)
            st.session_state["last_proposal"] = {"client_name": client_name, "project_type": project_type, "content": result}
    if st.session_state.get("last_proposal"):
        proposal = st.session_state["last_proposal"]
        st.text_area("Generated Proposal", proposal["content"], height=480)
        pdf = create_proposal_pdf(proposal["client_name"], proposal["project_type"], proposal["content"], profile)
        if pdf:
            st.download_button("Download Proposal as PDF", data=pdf, file_name=f"proposal_{proposal['client_name'].lower().replace(' ', '_')}.pdf", mime="application/pdf")
        else:
            st.warning("PDF export needs ReportLab. Run: pip install reportlab")


# ============================================================
# Page: Automations
# ============================================================

if current_page == "Automations":
    st.header("Automations")
    st.markdown("<div class='bf-card'><div class='bf-section-label'>5-Touch Proposal Automation</div>This control center handles proposal follow-up emails. Due automations can be generated, sent one-by-one, or sent in a batch.</div>", unsafe_allow_html=True)
    followup_tasks = [task for task in get_tasks() if task.get("category") == "proposal_followup"]
    counts = task_counts(followup_tasks)
    cols = st.columns(4)
    with cols[0]: metric_card("Overdue", counts.get("Overdue", 0), "#ff5c5c", "Past due")
    with cols[1]: metric_card("Due Today", counts.get("Due Today", 0), "#ffcc66", "Ready now")
    with cols[2]: metric_card("Upcoming", counts.get("Upcoming", 0), "#4ea1ff", "Next 7 days")
    with cols[3]: metric_card("Sent", counts.get("Sent", 0), "#35c46a", "Completed")
    st.divider()
    if st.button("Send All Due / Overdue Automated Follow-Ups"):
        results = send_all_due_followup_tasks()
        if not results:
            st.info("No due automations ready to send.")
        else:
            sent = sum(1 for _, ok, _ in results if ok)
            failed = len(results) - sent
            if sent:
                st.success(f"{sent} automated email(s) sent.")
            if failed:
                st.error(f"{failed} automated email(s) failed.")
            st.rerun()
    st.divider()
    if not followup_tasks:
        st.info("No automation tasks yet. Generate a proposal for a saved lead first.")
    else:
        for task in followup_tasks:
            lead = get_lead_by_id(int(task["related_id"])) if task.get("related_type") == "lead" else None
            label = f"{task_bucket(task)} — {task.get('title')} — {lead.get('client_name', 'Unknown Lead') if lead else 'Unknown Lead'} — Due {task.get('due_date')}"
            with st.expander(label):
                st.write(f"**Client:** {lead.get('client_name', '') if lead else ''}")
                st.write(f"**Email:** {lead.get('client_email', '') if lead else ''}")
                st.write(f"**Status:** {task.get('status', '')}")
                st.write(f"**Goal:** {task.get('description', '')}")
                if task.get("subject"):
                    st.write(f"**Subject:** {task.get('subject')}")
                if task.get("generated_content"):
                    st.text_area("Generated Email Body", task.get("generated_content"), height=220, key=f"automation_body_{task['id']}")
                if task.get("last_error"):
                    st.error(f"Last send error: {task.get('last_error')}")
                a1, a2, a3 = st.columns(3)
                with a1:
                    if st.button("Generate / Refresh Email", key=f"gen_task_{task['id']}"):
                        ok, msg = generate_task_email_content(int(task["id"]))
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
                        st.rerun()
                with a2:
                    if st.button("Send This Email", key=f"send_task_{task['id']}"):
                        ok, msg = send_task_email(int(task["id"]))
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
                        st.rerun()
                with a3:
                    if st.button("Skip This Step", key=f"skip_task_{task['id']}"):
                        mark_task_skipped(int(task["id"]))
                        st.success("Task skipped.")
                        st.rerun()


# ============================================================
# Page: Task Center
# ============================================================

if current_page == "Task Center":
    st.header("Task Center")
    st.markdown("<div class='bf-card'><div class='bf-section-label'>Unified Reminders</div>This page combines sales follow-up tasks, project kickoff tasks, and post-completion review/referral tasks in one place.</div>", unsafe_allow_html=True)
    all_tasks = get_tasks()
    counts = task_counts(all_tasks)
    cols = st.columns(4)
    with cols[0]: metric_card("Overdue", counts.get("Overdue", 0), "#ff5c5c", "Needs attention")
    with cols[1]: metric_card("Due Today", counts.get("Due Today", 0), "#ffcc66", "Action today")
    with cols[2]: metric_card("Upcoming", counts.get("Upcoming", 0), "#4ea1ff", "Next 7 days")
    with cols[3]: metric_card("Done / Sent", counts.get("Done", 0) + counts.get("Sent", 0), "#35c46a", "Completed")
    st.divider()
    if not all_tasks:
        st.info("No tasks yet.")
    else:
        status_filter = st.selectbox("Show Tasks", ["All", "Open Only", "Overdue", "Due Today", "Upcoming", "Completed"])
        filtered = []
        for task in all_tasks:
            bucket = task_bucket(task)
            if status_filter == "All": filtered.append(task)
            elif status_filter == "Open Only" and bucket in ["Overdue", "Due Today", "Upcoming", "Future"]: filtered.append(task)
            elif status_filter == "Completed" and bucket in ["Done", "Sent"]: filtered.append(task)
            elif status_filter == bucket: filtered.append(task)
        for task in filtered:
            title = f"{task_bucket(task)} — {task.get('title')} — Due {task.get('due_date')}"
            with st.expander(title):
                st.write(f"**Category:** {task.get('category')}")
                st.write(f"**Description:** {task.get('description')}")
                if task.get("related_type") == "lead":
                    lead = get_lead_by_id(int(task["related_id"]))
                    st.write(f"**Related Lead:** {lead.get('client_name', '') if lead else 'Missing'}")
                elif task.get("related_type") == "project":
                    project = get_project_by_id(int(task["related_id"]))
                    st.write(f"**Related Project:** {project.get('project_name', '') if project else 'Missing'}")
                b1, b2 = st.columns(2)
                with b1:
                    if task.get("status") not in ["Done", "Sent", "Skipped"] and st.button("Mark Done", key=f"done_task_{task['id']}"):
                        mark_task_done(int(task["id"]))
                        st.success("Task marked done.")
                        st.rerun()
                with b2:
                    if task.get("status") not in ["Done", "Sent", "Skipped"] and st.button("Skip Task", key=f"skip_center_{task['id']}"):
                        mark_task_skipped(int(task["id"]))
                        st.success("Task skipped.")
                        st.rerun()


# ============================================================
# Page: Project Pipeline
# ============================================================

if current_page == "Project Pipeline":
    st.header("Project Pipeline")
    projects = get_projects()
    if not projects:
        st.info("No projects yet. Mark a lead as Won to create its project record automatically.")
    else:
        active_count = sum(1 for project in projects if project.get("project_status") == "Active")
        completed_count = sum(1 for project in projects if project.get("project_status") == "Completed")
        pcols = st.columns(3)
        with pcols[0]: metric_card("Total Projects", len(projects), "#4ea1ff", "Won leads converted")
        with pcols[1]: metric_card("Active", active_count, "#d4af37", "In progress")
        with pcols[2]: metric_card("Completed", completed_count, "#35c46a", "Finished")
        st.divider()
        df = projects_to_dataframe(projects)
        st.dataframe(df, use_container_width=True)
        stage_counts = df["Stage"].value_counts().reset_index()
        stage_counts.columns = ["Stage", "Count"]
        dark_bar_chart(stage_counts, "Stage", "Count", "Projects by Stage")


# ============================================================
# Page: Project Details
# ============================================================

if current_page == "Project Details":
    st.header("Project Details")
    projects = get_projects()
    if not projects:
        st.info("No projects yet.")
    else:
        project = select_existing_project(projects, "project_details")
        if project:
            project_id = int(project["id"])
            c1, c2, c3 = st.columns(3)
            c1.metric("Stage", project.get("current_stage", ""))
            c2.metric("Status", project.get("project_status", ""))
            c3.metric("Value", f"${safe_float(project.get('estimated_value')):,.0f}")
            st.divider()
            left, right = st.columns(2)
            with left:
                st.subheader("Project Info")
                st.write(f"**Project:** {project.get('project_name', '')}")
                st.write(f"**Client:** {project.get('client_name', '')}")
                st.write(f"**Type:** {project.get('project_type', '')}")
                st.write(f"**Email:** {project.get('client_email', '') or 'Not provided'}")
                st.write(f"**Phone:** {project.get('client_phone', '') or 'Not provided'}")
                st.write(f"**Address:** {project.get('project_address', '') or 'Not provided'}")
            with right:
                st.subheader("Stage Management")
                current_stage = project.get("current_stage", PROJECT_STAGES[0])
                stage_index = PROJECT_STAGES.index(current_stage) if current_stage in PROJECT_STAGES else 0
                new_stage = st.selectbox("Project Stage", PROJECT_STAGES, index=stage_index)
                stage_note = st.text_area("Stage Note", placeholder="Example: Deposit received. Scheduling confirmed for next month.")
                if st.button("Update Project Stage"):
                    update_project_stage(project_id, new_stage, stage_note)
                    st.success("Project stage updated.")
                    st.rerun()
            st.divider()
            st.subheader("Project Notes")
            notes = st.text_area("Notes", value=project.get("notes", ""), height=140)
            if st.button("Save Project Notes"):
                edit_project_notes(project_id, notes)
                st.success("Project notes saved.")
                st.rerun()
            st.divider()
            st.subheader("Project Tasks")
            project_tasks = get_tasks_for_project(project_id)
            if not project_tasks:
                st.info("No project tasks yet.")
            else:
                task_df = pd.DataFrame([{"Task": t.get("title"), "Due": t.get("due_date"), "Status": t.get("status"), "Category": t.get("category")} for t in project_tasks])
                st.dataframe(task_df, use_container_width=True)
            st.divider()
            st.subheader("Project Timeline")
            events = get_project_events(project_id)
            if not events:
                st.info("No project events yet.")
            else:
                for event in events:
                    st.markdown(f"**{event.get('event_type')}** — {event.get('created_at')}")
                    st.write(event.get("note", ""))
                    st.divider()


# ============================================================
# Page: Growth Insights
# ============================================================

if current_page == "Growth Insights":
    st.header("Growth Insights")
    st.markdown(
        "<div class='bf-card'><div class='bf-section-label'>Strategy View</div>"
        "This section tracks which lead sources and project types are actually creating wins and won revenue. "
        "It belongs outside the main dashboard because it is for strategic decisions, not daily urgent action."
        "</div>",
        unsafe_allow_html=True
    )

    leads = get_leads()
    if not leads:
        st.info("Add and close leads to unlock useful growth insights.")
    else:
        source_df = group_analytics(leads, "lead_source")
        project_df = group_analytics(leads, "project_type")

        # Top-level insight cards
        card_cols = st.columns(2)

        if not source_df.empty:
            top_source = source_df.sort_values(["Won Value", "Win Rate %"], ascending=False).iloc[0]
            with card_cols[0]:
                metric_card(
                    "Top Lead Source",
                    str(top_source["Group"]),
                    "#4ea1ff",
                    f"Won value: ${safe_float(top_source['Won Value']):,.0f}"
                )
        else:
            with card_cols[0]:
                metric_card("Top Lead Source", "N/A", "#4ea1ff", "Not enough data")

        if not project_df.empty:
            top_project = project_df.sort_values(["Won Value", "Win Rate %"], ascending=False).iloc[0]
            with card_cols[1]:
                metric_card(
                    "Top Project Type",
                    str(top_project["Group"]),
                    "#35c46a",
                    f"Won value: ${safe_float(top_project['Won Value']):,.0f}"
                )
        else:
            with card_cols[1]:
                metric_card("Top Project Type", "N/A", "#35c46a", "Not enough data")

        st.divider()
        st.subheader("Lead Source Performance")
        st.dataframe(source_df, use_container_width=True)
        dark_bar_chart(source_df, "Group", ["Leads", "Won"], "Leads and Wins by Source")
        dark_bar_chart(source_df, "Group", "Won Value", "Won Value by Lead Source")

        st.divider()
        st.subheader("Project Type Performance")
        st.dataframe(project_df, use_container_width=True)
        dark_bar_chart(project_df, "Group", ["Leads", "Won"], "Leads and Wins by Project Type")
        dark_bar_chart(project_df, "Group", "Won Value", "Won Value by Project Type")

        st.divider()
        st.subheader("AI Growth Insight")
        growth_summary = f"""
Lead source analytics:
{source_df.to_string(index=False)}

Project type analytics:
{project_df.to_string(index=False)}
"""
        if st.button("Generate Growth Insight"):
            prompt = f"""
You are an analytics advisor for a remodeler/custom builder.

{company_context(profile)}

Use the data below to explain:
1. Which lead sources look strongest
2. Which project types look strongest
3. Which channels or services deserve more focus
4. One practical growth recommendation

Data:
{growth_summary}

Keep it clear and practical.
"""
            fallback = "Growth Insight\n\nThe strongest channels and project types will become clearer as more closed-won and closed-lost data is recorded. Prioritize the sources and services that create real won value, not just raw lead volume."
            insight = ai_generate(prompt, fallback)
            add_output("Growth Insight", "Growth Insights", insight)
            st.session_state["growth_insight"] = insight

        if st.session_state.get("growth_insight"):
            st.text_area("Growth Insight", st.session_state["growth_insight"], height=320)


# ============================================================
# Page: Client Update
# ============================================================

if current_page == "Client Update":
    st.header("Client Update")
    leads = get_leads()
    selected = None
    if leads:
        use_saved = st.checkbox("Use an existing saved lead/client", key="client_update_saved")
        if use_saved:
            selected = select_existing_lead(leads, "client_update")
    suffix = selected.get("id", "manual") if selected else "manual"
    with st.form(f"client_update_form_{suffix}"):
        client_name = st.text_input("Client Name", value=selected.get("client_name", "") if selected else "")
        project_name = st.text_input("Project Name", value=selected.get("project_type", "") if selected else "")
        client_email = st.text_input("Client Email", value=selected.get("client_email", "") if selected else "")
        progress_notes = st.text_area("Progress Notes")
        blockers = st.text_area("Blockers / Issues")
        next_steps = st.text_area("Next Steps")
        submitted = st.form_submit_button("Generate Client Update")
    if submitted:
        if not client_name or not project_name:
            st.error("Client name and project name are required.")
        else:
            result = ai_generate(build_client_update_prompt(profile, client_name, project_name, progress_notes, blockers, next_steps), fallback_client_update(profile, client_name, project_name, progress_notes, blockers, next_steps))
            add_output("Client Update", client_name, result, lead_id=int(selected["id"]) if selected else None)
            st.session_state["last_client_update"] = {"email": client_email, "subject": f"Project Update — {project_name}", "body": result}
    if st.session_state.get("last_client_update"):
        result = st.session_state["last_client_update"]
        st.text_area("Generated Client Update", result["body"], height=350)
        if result.get("email") and st.button("Send This Client Update Email Now"):
            ok, message = send_email_via_resend(result["email"], result["subject"], result["body"], profile)
            if ok:
                st.success("Email sent.")
            else:
                st.error(f"Email send failed: {message}")


# ============================================================
# Page: Referral / Review
# ============================================================

if current_page == "Referral / Review":
    st.header("Referral / Review")
    leads = get_leads()
    selected = None
    if leads:
        use_saved = st.checkbox("Use an existing saved lead/client", key="referral_saved")
        if use_saved:
            selected = select_existing_lead(leads, "referral")
    suffix = selected.get("id", "manual") if selected else "manual"
    with st.form(f"referral_form_{suffix}"):
        client_name = st.text_input("Client Name", value=selected.get("client_name", "") if selected else "")
        project_name = st.text_input("Project Name", value=selected.get("project_type", "") if selected else "")
        client_email = st.text_input("Client Email", value=selected.get("client_email", "") if selected else "")
        result_notes = st.text_area("Project Result Notes")
        submitted = st.form_submit_button("Generate Review / Referral Messages")
    if submitted:
        if not client_name or not project_name:
            st.error("Client name and project name are required.")
        else:
            result = ai_generate(build_referral_prompt(profile, client_name, project_name, result_notes), fallback_referral(profile, client_name, project_name, result_notes))
            add_output("Review / Referral", client_name, result, lead_id=int(selected["id"]) if selected else None)
            st.session_state["last_referral"] = {"email": client_email, "subject": f"Thank you for trusting us with your {project_name} project", "body": result}
    if st.session_state.get("last_referral"):
        result = st.session_state["last_referral"]
        st.text_area("Generated Review / Referral", result["body"], height=400)
        if result.get("email") and st.button("Send This Review / Referral Email Now"):
            ok, message = send_email_via_resend(result["email"], result["subject"], result["body"], profile)
            if ok:
                st.success("Email sent.")
            else:
                st.error(f"Email send failed: {message}")


# ============================================================
# Page: Feedback / Report Bug
# ============================================================

if current_page == "Feedback / Report Bug":
    st.header("Feedback / Report Bug")
    st.markdown(
        "<div class='bf-card'><div class='bf-section-label'>Beta Feedback Loop</div>"
        "This gives beta testers one simple place to report bugs, confusing areas, and feature ideas while using BuilderFlow."
        "</div>",
        unsafe_allow_html=True,
    )

    default_contact_email = profile.get("email", "") if profile else ""

    with st.form("feedback_form"):
        feedback_type = st.selectbox(
            "What are you sending?",
            [
                "Bug / Something Broke",
                "Confusing UX",
                "Feature Idea",
                "What I Liked",
                "General Feedback",
            ],
        )
        rating = st.slider("Overall BuilderFlow experience right now", min_value=1, max_value=10, value=8)
        message = st.text_area(
            "Feedback / Details",
            placeholder="Tell me what happened, what felt confusing, or what you would want improved.",
            height=180,
        )
        contact_email = st.text_input(
            "Email for follow-up, optional",
            value=default_contact_email,
        )
        submitted_feedback = st.form_submit_button("Submit Feedback")

    if submitted_feedback:
        if not message.strip():
            st.error("Please write a little feedback before submitting.")
        else:
            add_feedback(feedback_type, int(rating), message.strip(), contact_email.strip())
            st.success("Feedback submitted. Thank you — this is exactly what improves the beta.")

    st.divider()
    st.caption("In hosted beta mode, every tester's feedback is stored with their private workspace identity so you can review patterns later.")


# ============================================================
# Page: Saved Outputs
# ============================================================

if current_page == "Saved Outputs":
    st.header("Saved Outputs")
    outputs = get_outputs()
    if not outputs:
        st.info("No saved outputs yet.")
    else:
        for output in outputs:
            with st.expander(f"{output.get('output_type')} — {output.get('client_name')} — {output.get('created_at')}"):
                st.text_area("Saved Content", output.get("content", ""), height=300, key=f"saved_output_{output['id']}")
    st.divider()
    if st.button("Clear All Saved Outputs"):
        clear_outputs()
        st.success("Saved outputs cleared.")
        st.rerun()
def clean_ai_output(text: str) -> str:
    """Clean AI text so customer-facing emails/proposals do not show Markdown symbols."""
    if not text:
        return ""

    cleaned = str(text)

    replacements = {
        "**": "",
        "###": "",
        "##": "",
        "#": "",
        "__": "",
        "`": "",
    }

    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)

    cleaned = cleaned.replace("•", "-")
    cleaned = cleaned.replace("–", "-")
    cleaned = cleaned.replace("—", "-")

    lines = [line.rstrip() for line in cleaned.splitlines()]
    compact_lines = []
    previous_blank = False

    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and previous_blank:
            continue
        compact_lines.append(line)
        previous_blank = is_blank

    return "\n".join(compact_lines).strip()


def plain_text_ai_rule() -> str:
    return """
Formatting rules:
- Write in plain text only.
- Do not use Markdown formatting.
- Do not use ## headings.
- Do not use **bold** symbols.
- Do not use backticks.
- Use simple dashes for bullets when needed.
- Keep customer-facing messages clean and easy to copy into email.
"""

