"""Settings — configure the app without a text editor or a terminal (M8).

Every field here is backed by :mod:`services.settings_service`, which validates
against the same Pydantic constraints the app boots with, applies the change to
the running process, and persists it. There is no restart.

**Secrets are the exception, and deliberately so (D-19).** API keys and passwords
are displayed masked and cannot be edited here — they live in ``.env`` only. A
settings page that wrote keys to SQLite would put them in every backup of that
file, in every query output, and in whatever gets attached to a bug report. The
service enforces this at import time rather than trusting this page to behave.

**Each field shows where its value came from** — ``.env`` or a saved override —
because the first question anyone asks when a setting looks wrong is "wrong
compared to what?". An override can be reverted individually or in bulk.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from config import get_settings
from core.enums import UserRole
from core.exceptions import NewsletterAppError
from services.settings_service import SettingsService, SettingView, bounds
from ui import components, state

#: Groups rendered as tabs, in this order. Sourced from the registry's `group`.
GROUP_ORDER = ("AI", "Email", "Brand", "Content", "Operations")


def render() -> None:
    st.title("Settings")

    user = state.current_user()
    can_edit = bool(user and user.is_admin)

    if not can_edit:
        st.info(
            "You can see the configuration but not change it — that needs an admin account.",
            icon="🔒",
        )

    service = SettingsService()
    views = service.effective()
    by_group: dict[str, list[SettingView]] = {}
    for view in views:
        by_group.setdefault(view.spec.group, []).append(view)

    overridden = [v for v in views if v.is_overridden]
    if overridden:
        st.caption(
            f"{len(overridden)} setting{'s' if len(overridden) != 1 else ''} "
            "currently overriding .env — marked below."
        )

    names = [g for g in GROUP_ORDER if g in by_group]
    tab_names = [*names, "Account"]
    if can_edit:
        tab_names.append("Users")

    for tab, name in zip(st.tabs(tab_names), tab_names, strict=True):
        with tab:
            if name == "Account":
                _account_tab(user)
            elif name == "Users":
                _users_tab()
            else:
                _group_tab(service, name, by_group[name], can_edit=can_edit)


# ─────────────────────────────────────────────────────────────────────────────
#  A group of editable settings
# ─────────────────────────────────────────────────────────────────────────────
def _group_tab(
    service: SettingsService, group: str, views: list[SettingView], *, can_edit: bool
) -> None:
    if group == "AI":
        _model_capability_note()

    with st.form(f"settings_{group}"):
        pending: dict[str, Any] = {}
        for view in views:
            pending[view.spec.key] = _field(view, disabled=not can_edit)

        saved = st.form_submit_button(
            "Save changes", type="primary", disabled=not can_edit, width="stretch"
        )

    if saved:
        _save(service, pending, views)

    if group == "AI":
        _secret_row("LLM API key", get_settings().llm.api_key, "LLM_API_KEY / GROQ_API_KEY")
        _test_button("Test connection", service.test_llm, key=f"test_{group}")
    if group == "Email":
        settings = get_settings().email
        _secret_row("Brevo API key", settings.brevo_api_key, "BREVO_API_KEY")
        _secret_row("SMTP password", settings.smtp_password, "SMTP_PASSWORD")
        if settings.provider == "console":
            st.info(
                "The console provider writes `.eml` files to `data/outbox/` and sends "
                "nothing. Opening one in a real mail client is a better rendering test "
                "than the in-app preview.",
                icon="📥",
            )
        _test_button("Test connection", service.test_email, key=f"test_{group}")
    if group == "Brand":
        _compliance_check()

    _overrides_footer(service, views, can_edit=can_edit)


def _field(view: SettingView, *, disabled: bool) -> Any:
    """Render one setting as the widget its type calls for."""
    spec = view.spec
    label = spec.label
    help_text = f"{spec.help}  \n`{spec.env_var}`"
    key = f"set_{spec.key}"

    if view.is_overridden:
        label = f"{label} ●"
        help_text += f"  \nOverriding the .env value: `{view.env_value}`"

    if spec.kind == "choice":
        options = list(view.choices or ())
        index = options.index(str(view.value)) if str(view.value) in options else 0
        return st.selectbox(label, options, index=index, help=help_text, key=key, disabled=disabled)

    if spec.kind == "bool":
        return st.checkbox(
            label, value=bool(view.value), help=help_text, key=key, disabled=disabled
        )

    if spec.kind in ("int", "float"):
        low, high = bounds(spec)
        is_int = spec.kind == "int"
        return st.number_input(
            label,
            value=int(view.value) if is_int else float(view.value),
            min_value=(int(low) if is_int else float(low)) if low is not None else None,
            max_value=(int(high) if is_int else float(high)) if high is not None else None,
            step=1 if is_int else 0.1,
            help=help_text,
            key=key,
            disabled=disabled,
        )

    return st.text_input(label, value=str(view.value), help=help_text, key=key, disabled=disabled)


def _save(service: SettingsService, pending: dict[str, Any], views: list[SettingView]) -> None:
    """Persist only what actually changed, reporting each failure by field."""
    user = state.current_user()
    by_key = {view.spec.key: view for view in views}

    changed, failed = [], []
    for key, value in pending.items():
        if value == by_key[key].value:
            continue
        try:
            service.set(key, value, updated_by=user.username if user else None)
        except NewsletterAppError as exc:
            failed.append(exc)
        else:
            changed.append(by_key[key].spec.label)

    for exc in failed:
        components.error_panel(exc)

    if changed:
        st.success(f"Saved: {', '.join(changed)}. Active now — no restart needed.")
        st.rerun()
    elif not failed:
        st.info("Nothing changed.")


def _overrides_footer(
    service: SettingsService, views: list[SettingView], *, can_edit: bool
) -> None:
    overridden = [v for v in views if v.is_overridden]
    if not overridden:
        return

    st.divider()
    st.caption("● Overriding `.env`. Reverting restores the value from the file.")
    for view in overridden:
        left, right = st.columns([4, 1])
        with left:
            st.markdown(
                f"**{view.spec.label}** — `{view.value}` "
                f'<span class="muted">was `{view.env_value}`</span>',
                unsafe_allow_html=True,
            )
        with right:
            if st.button(
                "Revert",
                key=f"reset_{view.spec.key}",
                disabled=not can_edit,
                width="stretch",
            ):
                user = state.current_user()
                service.reset(view.spec.key, updated_by=user.username if user else None)
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
#  Secrets, connection tests, compliance
# ─────────────────────────────────────────────────────────────────────────────
def _masked(secret: object) -> str:
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    if not raw:
        return "not set"
    return f"••••••••{raw[-4:]}" if len(raw) > 8 else "••••"


def _secret_row(label: str, secret: object, env_var: str) -> None:
    """Show a secret masked, and say plainly why it is not editable."""
    st.markdown(
        f"**{label}** `{_masked(secret)}` "
        f'<span class="muted">— set `{env_var}` in `.env`. Secrets are never stored '
        "in the database (D-19).</span>",
        unsafe_allow_html=True,
    )


def _test_button(label: str, action, key: str) -> None:  # noqa: ANN001 - a zero-arg callable
    if st.button(label, type="primary", key=key):
        with st.spinner("Checking…"):
            result = action()
        if result.healthy:
            latency = f" ({result.latency_ms} ms)" if result.latency_ms else ""
            st.success(f"Connected — {result.detail}{latency}")
        else:
            st.error(result.detail)


def _model_capability_note() -> None:
    """Warn when the chosen model cannot guarantee schema-valid JSON."""
    from modules.ai.groq_provider import STRICT_SCHEMA_MODELS, supports_strict_schema

    llm = get_settings().llm
    if llm.provider != "mock" and not supports_strict_schema(llm.model):
        st.warning(
            f"`{llm.model}` does not support strict schema enforcement, so JSON validity "
            "is not guaranteed — the app falls back to a repair-retry, which is slower "
            "and occasionally fails. Models that do: "
            + ", ".join(f"`{m}`" for m in sorted(STRICT_SCHEMA_MODELS))
        )


def _compliance_check() -> None:
    """The two values that block sending outright."""
    brand = get_settings().brand
    st.markdown("**Legal requirements**")

    if brand.address.strip():
        st.success(f"Postal address set: {brand.address}")
    else:
        st.error(
            "**No postal address.** A physical address is legally required in marketing "
            "email. Sending is blocked until this is set."
        )

    if brand.unsubscribe_base_url.strip():
        st.success(f"Unsubscribe URL set: {brand.unsubscribe_base_url}")
    else:
        st.error("**No unsubscribe URL.** Every marketing email must carry one.")


# ─────────────────────────────────────────────────────────────────────────────
#  Account — change your own password
# ─────────────────────────────────────────────────────────────────────────────
def _account_tab(user: object) -> None:
    from services.auth_service import AuthService

    if user is None:
        st.info("Not signed in.")
        return

    st.markdown(f"**{user.display_name}** · `{user.username}` · {user.role}")
    st.divider()

    with st.form("change_password"):
        st.markdown("**Change your password**")
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")

        if not st.form_submit_button("Change password", type="primary"):
            return

        if new != confirm:
            st.error("The two new passwords don't match.")
            return

        auth = AuthService()
        try:
            # Re-authenticating proves the person at the keyboard is the account
            # holder and not someone who found an unlocked screen. It also runs
            # the lockout counter, so this cannot be used to brute-force the
            # current password from inside a session.
            auth.authenticate(user.username, current)
            auth.set_password(user.username, new)
        except NewsletterAppError as exc:
            components.error_panel(exc)
        else:
            st.success("Password changed. It takes effect the next time you sign in.")


# ─────────────────────────────────────────────────────────────────────────────
#  Users — admin only
# ─────────────────────────────────────────────────────────────────────────────
def _users_tab() -> None:
    from modules.repository.database import unit_of_work
    from modules.repository.user_repo import UserRepository
    from services.auth_service import AuthService

    current = state.current_user()

    st.markdown("**Accounts**")
    with unit_of_work() as session:
        users = UserRepository(session).list_all()

    for user in users:
        is_self = bool(current and user.username == current.username)
        with st.container(border=True):
            details, role, action = st.columns([3, 2, 2])
            with details:
                st.markdown(f"**{user.display_name}**" + (" *(you)*" if is_self else ""))
                st.markdown(
                    f'<span class="muted">{user.username} · {user.email}</span>',
                    unsafe_allow_html=True,
                )
            with role:
                st.markdown(f'<span class="muted">{user.role}</span>', unsafe_allow_html=True)
                if not user.is_active:
                    st.markdown('<span class="muted">deactivated</span>', unsafe_allow_html=True)
            with action:
                label = "Reactivate" if not user.is_active else "Deactivate"
                # Deactivating yourself logs you out with no way back in if you
                # are the only admin. Refused rather than warned about.
                if st.button(
                    label,
                    key=f"toggle_{user.username}",
                    width="stretch",
                    disabled=is_self,
                    help="You can't deactivate your own account." if is_self else None,
                ):
                    AuthService().set_active(user.username, active=not user.is_active)
                    st.rerun()

    st.divider()
    with st.form("add_user"):
        st.markdown("**Add a user**")
        username = st.text_input("Username")
        display_name = st.text_input("Display name")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        role = st.selectbox("Role", list(UserRole), format_func=lambda r: r.value)

        if st.form_submit_button("Create user", type="primary"):
            try:
                AuthService().create_user(
                    username=username,
                    display_name=display_name or username,
                    email=email,
                    password=password,
                    role=role,
                )
            except NewsletterAppError as exc:
                components.error_panel(exc)
            else:
                st.success(f"Created {username}.")
                st.rerun()
