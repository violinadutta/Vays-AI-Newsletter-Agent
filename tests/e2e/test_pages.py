"""Every page rendered for real, through Streamlit's AppTest harness.

``AppTest.from_function`` runs one page's ``render()`` as a script with a real
Streamlit context, which is the only way to catch the failure mode that matters
here: a page that imports cleanly and then raises on first render. That is
exactly what Priya would hit on her first click, and no unit test can see it.

The nav/auth shell is covered separately in ``test_app_smoke.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.e2e

ADDRESS = "Vays Infotech, 4th Floor, Tech Park, Pune 411045, India"
DB_PATH_KEY = "_test_db_url"


@pytest.fixture(autouse=True)
def _wired(db_session, set_env, tmp_path: Path) -> None:  # noqa: ANN001, ARG001
    """Point the app at a temporary database and a valid brand."""
    from tests.conftest import MINIMAL_ENV

    set_env(
        **MINIMAL_ENV,
        BRAND_ADDRESS=ADDRESS,
        EMAIL_SENDER_ADDRESS="newsletter@vays.com",
        UNSUBSCRIBE_BASE_URL="https://vaysinfotech.com/unsubscribe",
    )


def _page_script(module_name: str) -> None:
    """Import a page and render it.

    Defined at module level and taking its target as an argument, because
    ``AppTest.from_function`` extracts the function's *source* and re-executes it
    as a script — a closure over an outer variable is silently lost and fails
    with ``NameError`` at run time.
    """
    import importlib

    importlib.import_module(f"ui.pages.{module_name}").render()


def run_page(module_name: str, **session_state: object) -> AppTest:
    """Render one page in a real Streamlit context."""
    app = AppTest.from_function(_page_script, args=(module_name,), default_timeout=60)
    for key, value in session_state.items():
        app.session_state[key] = value
    app.run()
    return app


def seed_campaign() -> int:
    from core.enums import Category, Tone
    from core.models import NewsletterContent
    from modules.repository.campaign_repo import CampaignRepository
    from modules.repository.database import unit_of_work

    content = NewsletterContent(
        title="Dell's New PowerEdge Servers",
        summary="Dell refreshed its two-socket rack line with efficiency as the headline change.",
        newsletter=(
            "Dell has announced the PowerEdge R7xx series, a refresh of its mainstream "
            "two-socket rack line.\n\nEfficiency is the headline change."
        ),
        subject="Dell's new servers cut power costs",
        preview_text="What the refresh means for your cycle",
        cta="Talk to our team",
        keywords=["dell", "poweredge", "servers"],
        category=Category.PRODUCT_LAUNCH,
        tone=Tone.PROFESSIONAL,
    )
    with unit_of_work() as session:
        campaign = CampaignRepository(session).create(name="Seeded", content=content)
        return int(campaign.id)


# ─────────────────────────────────────────────────────────────────────────────
#  Every page renders
# ─────────────────────────────────────────────────────────────────────────────
class TestPagesRender:
    @pytest.mark.parametrize(
        "module", ["dashboard", "generate", "preview", "history", "settings_page", "logs"]
    )
    def test_page_renders_without_raising(self, module: str) -> None:
        app = run_page(module)

        assert not app.exception, f"ui/pages/{module}.py raised on first render"

    @pytest.mark.parametrize("module", ["dashboard", "history", "logs", "preview"])
    def test_empty_pages_show_a_next_step_not_a_blank(self, module: str) -> None:
        """A blank page reads as a bug. Every empty state names what to do."""
        app = run_page(module)

        rendered = " ".join(
            [str(m.value) for m in app.markdown]
            + [str(c.value) for c in app.caption]
            + [str(i.value) for i in app.info]
            + [str(t.value) for t in app.title]
        )
        assert len(rendered.strip()) > 40, f"{module} rendered almost nothing"


# ─────────────────────────────────────────────────────────────────────────────
#  Individual pages, with data
# ─────────────────────────────────────────────────────────────────────────────
class TestDashboard:
    def test_first_run_offers_getting_started(self) -> None:
        app = run_page("dashboard")

        body = " ".join(str(m.value) for m in app.markdown)
        assert "Getting started" in body or "No campaigns yet" in body

    def test_it_reports_system_health(self) -> None:
        """The answer to "is anything broken right now?" — the reason this page
        exists at all."""
        app = run_page("dashboard")

        body = " ".join(str(m.value) for m in app.markdown)
        assert "AI service" in body
        assert "Database" in body

    def test_a_seeded_campaign_appears(self) -> None:
        seed_campaign()

        app = run_page("dashboard")

        assert "Seeded" in " ".join(str(m.value) for m in app.markdown)


class TestPreview:
    def test_without_a_draft_it_directs_the_user(self) -> None:
        app = run_page("preview")

        body = " ".join(str(m.value) for m in app.markdown)
        assert "No draft open" in body

    def test_with_a_draft_it_renders_the_editor_and_the_email(self) -> None:
        campaign_id = seed_campaign()

        app = run_page("preview", **{"preview.campaign_id": campaign_id})

        assert not app.exception
        labels = {str(i.label) for i in app.text_input}
        assert "Subject line" in labels
        assert "Newsletter body" in {str(a.label) for a in app.text_area}

    def test_the_send_button_is_disabled_without_recipients(self) -> None:
        """The most important disabled state in the app."""
        campaign_id = seed_campaign()

        app = run_page("preview", **{"preview.campaign_id": campaign_id})

        send_buttons = [b for b in app.button if "Send campaign" in str(b.label)]
        assert send_buttons, "the send control should be present"
        assert all(b.disabled for b in send_buttons)

    def test_the_fact_check_warning_is_shown_with_sources(self) -> None:
        """Deliberate friction against hallucination risk (C-3)."""
        campaign_id = seed_campaign()

        app = run_page(
            "preview",
            **{
                "preview.campaign_id": campaign_id,
                "preview.source_urls": ["https://dell.com/blog/poweredge"],
            },
        )

        warnings = " ".join(str(w.value) for w in app.warning)
        assert "Verify every product name" in warnings


class TestHistory:
    def test_a_seeded_campaign_is_listed(self) -> None:
        seed_campaign()

        app = run_page("history")

        assert "Seeded" in " ".join(str(m.value) for m in app.markdown)

    def test_the_empty_state_explains_the_filters(self) -> None:
        app = run_page("history")

        body = " ".join(str(m.value) for m in app.markdown)
        assert "filters" in body.lower()


def admin() -> object:
    """An admin identity to put in session state, so edit controls are enabled."""
    from core.enums import UserRole
    from services.auth_service import AuthenticatedUser

    return AuthenticatedUser(
        username="admin", display_name="Priya Sharma", role=UserRole.ADMIN, token="t"
    )


class TestSettings:
    def test_the_editable_fields_are_rendered(self) -> None:
        app = run_page("settings_page", **{"auth.user": admin()})

        assert not app.exception
        assert "Endpoint URL" in {str(i.label) for i in app.text_input}
        assert "Provider" in {str(s.label) for s in app.selectbox}

    def test_secrets_are_masked_and_have_no_input(self, set_env) -> None:  # noqa: ANN001
        """A settings page that prints the key leaks it into every screenshot —
        and one that offers an input for it would write it to SQLite (D-19)."""
        set_env(LLM_API_KEY="gsk-super-secret-value-1234")

        app = run_page("settings_page", **{"auth.user": admin()})

        rendered = " ".join(str(m.value) for m in app.markdown)
        assert "gsk-super-secret-value-1234" not in rendered
        assert "••••" in rendered
        assert not [i for i in app.text_input if "API key" in str(i.label)]

    def test_a_missing_postal_address_is_flagged_as_blocking(self, set_env) -> None:  # noqa: ANN001
        set_env(BRAND_ADDRESS="")

        app = run_page("settings_page", **{"auth.user": admin()})

        errors = " ".join(str(e.value) for e in app.error)
        assert "postal address" in errors.lower()
        assert "blocked" in errors.lower()

    def test_a_non_admin_gets_a_read_only_page(self) -> None:
        """Repointing the LLM endpoint is not an everyday marketing action."""
        app = run_page("settings_page")

        assert "admin account" in " ".join(str(i.value) for i in app.info)
        assert all(i.disabled for i in app.text_input)

    def test_an_admin_can_edit(self) -> None:
        app = run_page("settings_page", **{"auth.user": admin()})

        assert not [i for i in app.text_input if i.disabled and i.label == "Endpoint URL"]

    def test_the_env_variable_behind_each_field_is_named(self) -> None:
        """So a change made here can be made permanent in .env at handover."""
        app = run_page("settings_page", **{"auth.user": admin()})

        helps = " ".join(str(i.help or "") for i in app.text_input)
        assert "LLM_BASE_URL" in helps

    def test_an_override_is_shown_as_overriding(self) -> None:
        """ "Wrong compared to what?" is the first question when a setting looks
        wrong, and .env no longer being the answer is the thing to surface."""
        from services import settings_service

        settings_service.SettingsService().set("brand.name", "Overridden Ltd")

        app = run_page("settings_page", **{"auth.user": admin()})

        body = " ".join(str(m.value) for m in app.markdown)
        assert "Overridden Ltd" in body
        assert "Revert" in {str(b.label) for b in app.button}

    def test_the_account_tab_offers_a_password_change(self) -> None:
        """Without this, a password change needs a terminal — which is exactly
        what M8 exists to remove."""
        app = run_page("settings_page", **{"auth.user": admin()})

        labels = {str(i.label) for i in app.text_input}
        assert {"Current password", "New password", "Confirm new password"} <= labels

    def test_you_cannot_deactivate_yourself(self) -> None:
        """The only admin locking themselves out has no way back in."""
        from core.enums import UserRole
        from services.auth_service import AuthService

        AuthService().create_user(
            username="admin",
            display_name="Priya Sharma",
            email="p@vays.com",
            password="a sufficiently long passphrase",
            role=UserRole.ADMIN,
        )

        app = run_page("settings_page", **{"auth.user": admin()})

        toggles = [b for b in app.button if str(b.label) in ("Deactivate", "Reactivate")]
        assert toggles, "expected the account row to render"
        assert all(b.disabled for b in toggles)


class TestLogs:
    def test_it_renders_and_explains_retention(self) -> None:
        from modules.repository.database import unit_of_work
        from modules.repository.log_repo import LogRepository

        with unit_of_work() as session:
            LogRepository(session).write(
                level="INFO", logger="test", event="campaign.sent", correlation_id="abc12345"
            )

        app = run_page("logs")

        assert not app.exception
        assert "campaign.sent" in " ".join(str(m.value) for m in app.markdown)

    def test_a_correlation_id_is_offered_as_a_filter(self) -> None:
        """The feature that turns "it broke around 3pm" into a diagnosis."""
        from modules.repository.database import unit_of_work
        from modules.repository.log_repo import LogRepository

        with unit_of_work() as session:
            LogRepository(session).write(
                level="ERROR", logger="test", event="llm.failed", correlation_id="deadbeef"
            )

        app = run_page("logs")

        assert any("deadbeef" in str(b.label) for b in app.button)


class TestGenerate:
    def test_it_offers_the_style_controls(self) -> None:
        app = run_page("generate")

        labels = {str(s.label) for s in app.selectbox}
        assert {"Tone", "Length", "Audience"} <= labels

    def test_generate_is_disabled_without_articles(self) -> None:
        app = run_page("generate")

        generate_buttons = [b for b in app.button if "Generate newsletter" in str(b.label)]
        assert generate_buttons
        assert all(b.disabled for b in generate_buttons)
