# ADR 0002 — Hand-authored table HTML instead of MJML

**Status:** Accepted · **Date:** 2026-08-07 · **Supersedes:** the templating half of D-11

## Context

MJML was selected for email templates because it compiles a clean component syntax
into the table-based HTML that Outlook's Word rendering engine requires — writing
that by hand is famously unpleasant.

MJML's compiler is a **Node package**. Node is not installed on the development
machine, and the deliverable is explicitly a pure-Python, Windows-first handover.

That left `.mjml` sources in the repository that **could not be compiled by anyone
who received the project**. An uncompilable source of truth is worse than none: the
next developer would edit the `.mjml`, see no change, and lose an afternoon.

## Decision

Delete the MJML sources. **Hand-author the table HTML** and render it with Jinja2,
inlining CSS at render time with `premailer` (pure Python).

The rules MJML would have enforced are enforced by tests instead:

- Tables for layout, never divs (`role="presentation"` so screen readers skip them)
- Fixed 600px width, explicit on every table
- No flexbox, grid, float or `background-image`
- CTA is a padded table cell, not a `<button>`
- MSO conditional wrapper for Outlook
- CSS inlined, because Gmail strips `<style>` blocks

24 template tests assert these across all three layouts, parametrised so a new
template is covered automatically.

## Consequences

Good:
- **Zero Node dependency at any stage.** The last one is gone
- The HTML in the repository is the HTML that ships — what you read is what sends
- A new template is a copy-paste-edit, no build step

Bad:
- The templates are verbose and must be edited carefully; the discipline MJML
  enforced is now a review responsibility
- **Structural tests are not visual verification.** They prove the markup has the
  right properties, not that Outlook draws it correctly. That gap is real and is
  recorded in KNOWN_ISSUES § 1.2 — closing it needs Litmus or the actual clients
