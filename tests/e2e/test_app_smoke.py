"""End-to-end smoke tests for the application shell.

These run the real ``app.py`` through Streamlit's ``AppTest`` harness — no
browser, no server. They exist to catch the class of failure unit tests cannot:
the app importing cleanly but crashing on first render, or the auth guard being
bypassable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.e2e

APP = str(Path(__file__).resolve().parents[2] / "app.py")
PASSWORD = "correct horse battery staple"


@pytest.fixture
def app(db_session, set_env, tmp_path) -> AppTest:  # noqa: ANN001
    """A fresh app instance wired to a temporary database and a known user."""
    from core.enums import UserRole
    from services.auth_service import AuthService
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)
    AuthService().create_user(
        username="priya",
        display_name="Priya Sharma",
        email="priya@vays.com",
        password=PASSWORD,
        role=UserRole.ADMIN,
    )
    return AppTest.from_file(APP, default_timeout=30)


class TestAuthGuard:
    def test_unauthenticated_visitors_see_only_the_login_form(self, app: AppTest) -> None:
        """The single most important assertion in this file: no campaign data
        renders before a successful sign-in (NFR-S5)."""
        app.run()

        assert not app.exception
        assert any("Sign in" in str(b.label) for b in app.button)
        assert not app.sidebar.button  # no sign-out, so no session

    def test_empty_credentials_are_rejected_with_a_prompt(self, app: AppTest) -> None:
        app.run()
        app.button[0].click().run()

        assert any("Enter both" in str(e.value) for e in app.error)

    def test_wrong_credentials_show_a_generic_error(self, app: AppTest) -> None:
        app.run()
        app.text_input[0].set_value("priya").run()
        app.text_input[1].set_value("definitely not the password").run()
        app.button[0].click().run()

        assert any("Incorrect username or password" in str(e.value) for e in app.error)

    def test_the_error_does_not_reveal_whether_the_user_exists(self, app: AppTest) -> None:
        app.run()
        app.text_input[0].set_value("no-such-person").run()
        app.text_input[1].set_value("whatever").run()
        app.button[0].click().run()

        messages = " ".join(str(e.value) for e in app.error)
        assert "Incorrect username or password" in messages
        assert "not found" not in messages.lower()


class TestAuthenticatedShell:
    def _login(self, app: AppTest) -> AppTest:
        app.run()
        app.text_input[0].set_value("priya").run()
        app.text_input[1].set_value(PASSWORD).run()
        app.button[0].click().run()
        return app

    def test_a_valid_login_renders_the_shell(self, app: AppTest) -> None:
        self._login(app)

        assert not app.exception
        assert any("Priya Sharma" in str(m.value) for m in app.sidebar.markdown)

    def test_the_shell_offers_sign_out(self, app: AppTest) -> None:
        self._login(app)

        assert any("Sign out" in str(b.label) for b in app.sidebar.button)

    def test_signing_out_returns_to_the_login_form(self, app: AppTest) -> None:
        self._login(app)
        next(b for b in app.sidebar.button if "Sign out" in str(b.label)).click().run()

        assert any("Sign in" in str(b.label) for b in app.button)


class TestBranding:
    """The logo in the top-left corner."""

    def test_the_dark_ui_variant_exists_and_is_a_png(self) -> None:
        """The original wordmark is near-black and vanishes on the navy sidebar,
        so a light variant is what actually gets shown."""
        from config.constants import ASSETS_DIR

        variant = ASSETS_DIR / "logo-dark-ui.png"

        assert variant.is_file()
        assert variant.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_the_variant_keeps_the_original_dimensions(self) -> None:
        """A recolour, not a resize — the mark must not be subtly distorted."""
        import struct

        from config.constants import ASSETS_DIR

        def size(name: str) -> tuple[int, int]:
            raw = (ASSETS_DIR / name).read_bytes()
            return struct.unpack(">II", raw[16:24])

        assert size("logo-dark-ui.png") == size("image001.png")

    def test_a_missing_logo_is_handled_rather_than_raising(self, tmp_path) -> None:  # noqa: ANN001
        """Branding must never be the reason the dashboard fails to render.

        Note this imports ``ui.styles``, not ``app``: importing ``app`` executes
        the whole application at module scope, which pollutes global state for
        every test that runs afterwards.
        """
        from ui import styles

        assert styles.dashboard_logo(tmp_path) is None

    def test_the_dark_variant_is_preferred(self) -> None:
        from ui import styles

        assert styles.dashboard_logo().name == "logo-dark-ui.png"


class TestSidebarHealth:
    """The sidebar dot must agree with the Dashboard panel.

    It used to pass ``provider == "mock"`` as the health flag — a placeholder
    from before the health checks existed. That made the dot report *which
    provider is configured*, not whether it works: a perfectly good Groq setup
    read "offline" in the sidebar while the Dashboard read "online" two inches
    away, and people believed the sidebar.

    The discriminating case is a provider that is configured but **not healthy**.
    The old code called that online; the new code calls it offline.
    """

    def _signed_in(self, app: AppTest) -> AppTest:
        app.run()
        app.text_input[0].set_value("priya").run()
        app.text_input[1].set_value(PASSWORD).run()
        app.button[0].click().run()
        return app

    @staticmethod
    def _patch_health(monkeypatch, *, healthy: bool) -> None:  # noqa: ANN001
        from core.models import HealthStatus
        from services.health_service import HealthService, SystemHealth

        monkeypatch.setattr(
            HealthService,
            "check",
            lambda _self, **_kw: SystemHealth(
                llm=HealthStatus(healthy=healthy, detail="probe"),
                database=HealthStatus(healthy=True, detail="ok"),
                email=HealthStatus(healthy=healthy, detail="probe"),
            ),
        )

    def test_an_unhealthy_service_reads_offline(self, app: AppTest, monkeypatch) -> None:  # noqa: ANN001
        """The case the old code got wrong. It reported the mock provider as
        online regardless of whether anything actually answered."""
        self._patch_health(monkeypatch, healthy=False)

        sidebar = " ".join(str(m.value) for m in self._signed_in(app).sidebar.markdown)

        assert "AI service" in sidebar
        assert "offline" in sidebar

    def test_a_healthy_service_reads_online(self, app: AppTest, monkeypatch) -> None:  # noqa: ANN001
        self._patch_health(monkeypatch, healthy=True)

        sidebar = " ".join(str(m.value) for m in self._signed_in(app).sidebar.markdown)

        assert "AI service" in sidebar
        assert "offline" not in sidebar

    def test_a_failing_health_check_does_not_break_the_shell(
        self, app: AppTest, monkeypatch
    ) -> None:  # noqa: ANN001
        """A status dot is the least important thing on the page. It must never
        be the reason nothing renders — and neither must the Dashboard panel,
        which is the page whose whole job is to report faults."""
        from services.health_service import HealthService

        def boom(*_a: object, **_k: object) -> None:
            msg = "the probe itself failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(HealthService, "check", boom)

        result = self._signed_in(app)

        assert not result.exception
