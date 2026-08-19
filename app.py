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
from ui import session as session_store
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
                    user = auth.authenticate(username, password)
                    state.set_current_user(user)
                    session_store.remember(user.username, user.role, user.token)
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
        # Real probes, shared with the Dashboard panel through one cached
        # service. Until now this reported `provider == "mock"` as "online" — a
        # placeholder from before the health checks existed, which meant a
        # perfectly working Groq setup showed "offline" in the sidebar while the
        # Dashboard showed "online" two inches away.
        llm_provider = settings.llm.provider  # type: ignore[attr-defined]
        email_provider = settings.email.provider  # type: ignore[attr-defined]
        try:
            health = state.health_service().check()  # type: ignore[attr-defined]
            llm_ok, email_ok = health.llm.healthy, health.email.healthy
        except Exception:  # noqa: BLE001 - a status dot must never break the shell
            llm_ok = email_ok = False

        st.markdown(
            styles.health_dot(llm_ok, f"AI service ({llm_provider})"), unsafe_allow_html=True
        )
        st.markdown(
            styles.health_dot(email_ok, f"Email ({email_provider})"), unsafe_allow_html=True
        )
        st.divider()

        st.markdown(f"**{user.display_name}**")
        st.markdown(f'<span class="muted">{user.role.value}</span>', unsafe_allow_html=True)
        if st.button("Sign out", width="stretch"):
            get_logger(__name__).info("auth.logout", username=user.username)
            session_store.forget()
            state.logout()
            st.rerun()


def render_logo() -> None:
    """Brand mark in the top-left, above the navigation.

    ``st.logo`` rather than an image inside the sidebar: it pins the mark to the
    corner and keeps it there across every page, which is what a logo is for.

    The dark-UI variant is used when one exists. The original wordmark is
    near-black and would be invisible on the navy sidebar — the same trap the
    email header hit. A missing file falls back to the original, and a missing
    original is skipped entirely: branding must never be what stops the app
    rendering.
    """
    logo = styles.dashboard_logo()
    if logo is not None:
        st.logo(str(logo), size="large", link=get_settings().brand.website or None)


def main() -> None:
    styles.inject()

    context = bootstrap()
    if not context.get("ok"):
        # Deliberately no logo here. It reads the brand settings, and the reason
        # this branch was taken may be that settings are unreadable — a config
        # problem must produce the setup page, not a traceback from the banner
        # above it.
        render_startup_error(context.get("error"))
        return

    auth: AuthService = context["auth"]  # type: ignore[assignment]
    settings = context["settings"]

    # Before the auth gate, so the login screen carries the brand too.
    render_logo()

    # ── the one public route ────────────────────────────────────────────────
    # Recipients are customers. They have no account and never will, so the Like
    # and Unsubscribe links in a newsletter cannot be served from behind the
    # login. This is the only hole in the auth gate and it is kept deliberately
    # narrow:
    #
    #   * it opens only when a `t` parameter is present, and the page refuses
    #     anything whose HMAC signature does not verify against APP_SECRET_KEY;
    #   * the token names one address, one campaign and one action, so it grants
    #     nothing beyond itself — there is no session, no navigation and no
    #     access to any other page;
    #   * it returns immediately, so no admin page can render on this path even
    #     if a later edit adds one.
    from ui.pages import engage

    if engage.is_recipient_link():
        engage.render()
        return

    # A browser refresh starts a new Streamlit session, so `session_state` is
    # empty even though the person never signed out. Rebuild from the cookie
    # before concluding they are anonymous.
    if not state.is_authenticated():
        restored = session_store.restore()
        if restored is not None:
            state.set_current_user(restored)  # type: ignore[arg-type]

    if not state.is_authenticated():
        render_login(auth)
        return

    render_sidebar(settings)

    # The page objects live in ui/nav.py so that a page can navigate to another
    # page by name. st.switch_page needs the object, not the url_path.
    from ui import nav

    navigation = st.navigation(nav.build())
    navigation.run()


main()
