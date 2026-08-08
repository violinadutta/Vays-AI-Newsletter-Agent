"""Streamlit entry point.

Responsibilities, in order:

1. Bootstrap once per process — settings, logging, database, log pruning.
2. Gate everything behind authentication.
3. Render the shell (sidebar, health indicators) and hand off to a page.

Deliberately thin. Business logic lives in ``services/``; this file only decides
*what to show*. Run it with::

    streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from config import configure_logging, get_logger, get_settings
from core.exceptions import ConfigurationError, NewsletterAppError
from services.auth_service import AuthService
from ui import state, styles

st.set_page_config(
    page_title="Vays Newsletter Platform",
    page_icon="📰",  # an emoji, not a path — a missing asset file must not break startup
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def bootstrap() -> dict[str, object]:
    """One-time process startup.

    ``cache_resource`` rather than plain module code: Streamlit re-executes this
    script on every interaction, and re-initialising the database and log
    handlers on each click would be both wasteful and wrong (duplicate handlers,
    repeated log pruning).

    Returns a dict rather than raising, so a configuration problem can be
    rendered as a readable page instead of a traceback in the browser.
    """
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        return {"ok": False, "error": exc}

    try:
        from modules.repository.database import init_database, unit_of_work
        from modules.repository.log_handler import attach_db_log_handler
        from modules.repository.log_repo import LogRepository
        from services.settings_service import SettingsService

        init_database()

        # Saved overrides are applied *before* logging is configured, because
        # `logging.log_level` is one of them and `configure_logging` reads the
        # level once. Applying afterwards would boot at the .env level and
        # silently ignore the saved one.
        overrides = SettingsService().apply_saved()

        configure_logging(settings.logging.log_level)
        log = get_logger(__name__)
        if overrides:
            log.info("settings.overrides_active", count=overrides)

        with unit_of_work() as session:
            pruned = LogRepository(session).prune()
        if pruned:
            log.info("logs.pruned", rows=pruned)

        # Attached only after the database exists, so a startup failure is still
        # visible in the console and the log file rather than swallowed.
        attach_db_log_handler()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user below
        configure_logging(settings.logging.log_level)
        get_logger(__name__).exception("startup.database_failed")
        return {"ok": False, "error": exc}

    log.info(
        "app.started",
        env=settings.app.env,
        llm_provider=settings.llm.provider,
        email_provider=settings.email.provider,
    )
    return {"ok": True, "settings": settings, "auth": AuthService()}


def render_startup_error(error: object) -> None:
    """Show a configuration or startup failure as instructions, not a traceback."""
    st.title("Setup needed")
    if isinstance(error, NewsletterAppError):
        st.error(error.user_message)
        with st.expander("Technical detail"):
            st.code(error.message)
    else:
        st.error("The application could not start. See the detail below.")
        st.code(str(error))

    st.markdown(
        "**Common fixes**\n\n"
        "1. Copy `.env.example` to `.env` and fill in the required values.\n"
        "2. Generate a secret key: "
        '`python -c "import secrets; print(secrets.token_urlsafe(48))"`\n'
        "3. Create the database tables: `alembic upgrade head`\n"
        "4. Create your first user: `python -m scripts.create_user`"
    )


def render_login(auth: AuthService) -> None:
    """Login screen. Nothing else renders until this succeeds."""
    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        st.title("Vays Newsletter Platform")
        st.caption("Sign in to continue")

        with st.form("login", clear_on_submit=False):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

        if submitted:
            if not username or not password:
                state.set_value(state.LOGIN_ERROR, "Enter both your username and password.")
            else:
                try:
                    state.set_current_user(auth.authenticate(username, password))
                    state.clear(state.LOGIN_ERROR)
                    st.rerun()
                except NewsletterAppError as exc:
                    state.set_value(state.LOGIN_ERROR, exc.user_message)

        error = state.get(state.LOGIN_ERROR)
        if error:
            st.error(error)

        if not auth.has_any_users():
            st.info(
                "No accounts exist yet. Create the first one from a terminal:\n\n"
                "`python -m scripts.create_user`"
            )


def render_sidebar(settings: object) -> None:
    """Sidebar: identity, role, service health, sign-out."""
    user = state.current_user()
    assert user is not None  # noqa: S101 - render_sidebar is only reached authenticated

    with st.sidebar:
        st.markdown("### Vays Newsletter")
        st.divider()

        # Health probes land in M3/M6; until then the indicators report what is
        # configured, which is still the answer to "why isn't generation working?"
        llm_provider = settings.llm.provider  # type: ignore[attr-defined]
        email_provider = settings.email.provider  # type: ignore[attr-defined]
        st.markdown(
            styles.health_dot(llm_provider == "mock", f"AI service ({llm_provider})"),
            unsafe_allow_html=True,
        )
        st.markdown(
            styles.health_dot(True, f"Email ({email_provider})"),
            unsafe_allow_html=True,
        )
        st.divider()

        st.markdown(f"**{user.display_name}**")
        st.markdown(f'<span class="muted">{user.role.value}</span>', unsafe_allow_html=True)
        if st.button("Sign out", width="stretch"):
            get_logger(__name__).info("auth.logout", username=user.username)
            state.logout()
            st.rerun()


def main() -> None:
    styles.inject()

    context = bootstrap()
    if not context.get("ok"):
        render_startup_error(context.get("error"))
        return

    auth: AuthService = context["auth"]  # type: ignore[assignment]
    settings = context["settings"]

    if not state.is_authenticated():
        render_login(auth)
        return

    render_sidebar(settings)

    from ui.pages import dashboard, generate, history, logs, preview, settings_page

    # `url_path` is explicit on every page. Streamlit otherwise infers the path
    # from the callable's name, and all six pages expose `render()` — which
    # collides into a single "/render" and raises StreamlitAPIException.
    navigation = st.navigation(
        [
            st.Page(dashboard.render, title="Dashboard", icon=":material/dashboard:", default=True),
            st.Page(
                generate.render,
                title="Generate Newsletter",
                icon=":material/auto_awesome:",
                url_path="generate",
            ),
            st.Page(
                preview.render,
                title="Campaign Preview",
                icon=":material/preview:",
                url_path="preview",
            ),
            st.Page(
                history.render,
                title="Campaign History",
                icon=":material/history:",
                url_path="history",
            ),
            st.Page(
                settings_page.render,
                title="Settings",
                icon=":material/settings:",
                url_path="settings",
            ),
            st.Page(logs.render, title="Logs", icon=":material/receipt_long:", url_path="logs"),
        ]
    )
    navigation.run()


main()
