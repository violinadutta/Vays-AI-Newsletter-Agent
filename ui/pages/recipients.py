"""Recipients — the standing mailing list, managed by hand.

**The list persists.** A CSV is imported once and stays; later uploads *append*
rather than replace, and addresses can be added or removed here without touching
a file. Replacing on upload would be one mis-click away from losing a list
nobody has another copy of.

Removal deactivates rather than deletes, so re-importing the original file
cannot quietly undo a deliberate removal. Unsubscribes are separate and outrank
this list entirely — someone who opted out is never sent to, whatever it says
here.
"""

from __future__ import annotations

import streamlit as st

from core.enums import UserRole
from core.exceptions import NewsletterAppError
from services.subscriber_service import SubscriberService, parse_pasted_addresses
from ui import components, state


def render() -> None:
    st.title("Recipients")

    service = SubscriberService()
    user = state.current_user()
    can_edit = bool(user and user.role in {UserRole.ADMIN, UserRole.APPROVER, UserRole.EDITOR})

    counts = service.counts()
    _header(counts)

    if not can_edit:
        st.info("Sign in to add or remove recipients.", icon="🔒")

    add, upload, manage = st.tabs(["Add", "Import a CSV", "Manage list"])
    with add:
        _add_tab(service, user, enabled=can_edit)
    with upload:
        _import_tab(service, user, enabled=can_edit)
    with manage:
        _manage_tab(service, user, counts, enabled=can_edit)


def _header(counts: dict[str, int]) -> None:
    active, inactive = counts.get("active", 0), counts.get("inactive", 0)
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"### {active:,} active")
        st.caption(
            "Everyone a campaign goes to. Unsubscribed addresses are skipped at "
            "send time even if they appear here."
        )
    with right:
        if inactive:
            st.markdown(f"**{inactive:,} removed**")
            st.caption("Kept on record so a re-import cannot bring them back.")

    if not active:
        components.empty_state(
            "No recipients yet",
            "Import a CSV with an 'email' column, or add addresses by hand. "
            "The list is saved and grows — you only upload once.",
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Add by hand
# ─────────────────────────────────────────────────────────────────────────────
def _add_tab(service: SubscriberService, user: object, *, enabled: bool) -> None:
    st.markdown("**Add one person**")
    with st.form("add_one", clear_on_submit=True):
        email = st.text_input("Email address", disabled=not enabled)
        name_col, company_col = st.columns(2)
        with name_col:
            name = st.text_input("Name (optional)", disabled=not enabled)
        with company_col:
            company = st.text_input("Company (optional)", disabled=not enabled)

        if st.form_submit_button("Add to list", type="primary", disabled=not enabled):
            _add_one(service, user, email, name, company)

    st.divider()
    st.markdown("**Or paste several**")
    st.caption("Separated by commas, spaces or new lines — however they came to you.")

    with st.form("add_many", clear_on_submit=True):
        pasted = st.text_area(
            "Addresses",
            height=120,
            placeholder="priya@acme.com, rahul@beta.com\nsam@gamma.com",
            disabled=not enabled,
        )
        if st.form_submit_button("Add all", disabled=not enabled):
            _add_pasted(service, user, pasted)


def _add_one(service: SubscriberService, user: object, email: str, name: str, company: str) -> None:
    if not email.strip():
        st.warning("Enter an email address.")
        return
    try:
        outcome = service.add(
            email,
            name=name,
            company=company,
            added_by=getattr(user, "username", None),
        )
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return

    if outcome.added:
        st.success(f"Added {email.strip().lower()}.")
    elif outcome.reactivated:
        st.success(f"{email.strip().lower()} was previously removed and is back on the list.")
    else:
        st.info(f"{email.strip().lower()} is already on the list.")
    st.rerun()


def _add_pasted(service: SubscriberService, user: object, pasted: str) -> None:
    addresses, invalid = parse_pasted_addresses(pasted)
    if not addresses and not invalid:
        st.warning("Nothing to add.")
        return

    added = restored = existing = 0
    for address in addresses:
        try:
            outcome = service.add(address, added_by=getattr(user, "username", None))
        except NewsletterAppError as exc:
            invalid[address] = exc.user_message
            continue
        added += outcome.added
        restored += outcome.reactivated
        existing += outcome.already_present

    if added or restored:
        st.success(f"{added} added, {restored} restored, {existing} already on the list.")
    elif existing:
        st.info(f"All {existing} were already on the list.")

    if invalid:
        with st.expander(f"{len(invalid)} could not be added"):
            for value, reason in invalid.items():
                st.markdown(f"- `{value}` — {reason}")
    if added or restored:
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
#  Import
# ─────────────────────────────────────────────────────────────────────────────
def _import_tab(service: SubscriberService, user: object, *, enabled: bool) -> None:
    st.info(
        "Importing **adds to** the list — it never replaces it. Uploading the same "
        "file twice is safe: nobody is added twice, and anyone you removed stays "
        "removed.",
        icon="ℹ️",
    )

    upload = st.file_uploader(
        "Recipient CSV",
        type=["csv"],
        help="Needs a column named 'email'. 'name' and 'company' are used for personalisation.",
        disabled=not enabled,
    )

    st.download_button(
        "Download a sample file",
        "email,name,company\npriya@example.com,Priya Sharma,Acme Ltd\n",
        file_name="recipients-sample.csv",
        mime="text/csv",
    )

    if upload is None:
        return

    try:
        report = service.import_csv(
            upload.getvalue(),
            source=f"csv:{upload.name}"[:64],
            added_by=getattr(user, "username", None),
        )
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return

    if report.outcome.changed:
        st.success(report.summary)
    else:
        st.info(report.summary)

    if report.invalid:
        with st.expander(f"{len(report.invalid)} rows were rejected"):
            for row, reason in report.invalid.items():
                st.markdown(f"- `{row}` — {reason}")
    if report.suppressed:
        with st.expander(f"{len(report.suppressed)} are unsubscribed and were skipped"):
            st.markdown("\n".join(f"- `{a}`" for a in report.suppressed))


# ─────────────────────────────────────────────────────────────────────────────
#  Manage
# ─────────────────────────────────────────────────────────────────────────────
def _manage_tab(
    service: SubscriberService, user: object, counts: dict[str, int], *, enabled: bool
) -> None:
    if not counts.get("total"):
        st.caption("Nothing on the list yet.")
        return

    search_col, filter_col = st.columns([3, 2])
    with search_col:
        term = st.text_input("Search", placeholder="name, email or company…")
    with filter_col:
        show_removed = st.checkbox("Include removed", value=False)

    rows = service.search(term, include_inactive=show_removed, limit=200)
    if not rows:
        st.caption("Nobody matches that search.")
        return

    st.caption(f"Showing {len(rows)} of {counts.get('total', 0):,}.")

    for row in rows:
        _row(service, user, row, enabled=enabled)

    st.divider()
    st.download_button(
        "Export the active list (CSV)",
        service.export_csv(),
        file_name="recipients.csv",
        mime="text/csv",
        help="A backup, or a copy for another tool.",
    )


def _row(service: SubscriberService, user: object, row: dict, *, enabled: bool) -> None:
    with st.container(border=True):
        details, meta, action = st.columns([4, 3, 2])

        with details:
            label = row["name"] or row["email"]
            st.markdown(f"**{label}**" + ("" if row["is_active"] else " · *removed*"))
            if row["name"]:
                st.markdown(f'<span class="muted">{row["email"]}</span>', unsafe_allow_html=True)
        with meta:
            bits = [row["company"] or "—", row["source"]]
            st.markdown(f'<span class="muted">{" · ".join(bits)}</span>', unsafe_allow_html=True)
        with action:
            username = getattr(user, "username", None)
            if row["is_active"]:
                if st.button(
                    "Remove", key=f"rm_{row['email']}", disabled=not enabled, width="stretch"
                ):
                    service.remove(row["email"], removed_by=username)
                    st.rerun()
            elif st.button(
                "Restore", key=f"re_{row['email']}", disabled=not enabled, width="stretch"
            ):
                service.restore(row["email"], restored_by=username)
                st.rerun()
