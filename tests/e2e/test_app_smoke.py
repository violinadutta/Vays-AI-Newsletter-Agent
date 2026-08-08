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
