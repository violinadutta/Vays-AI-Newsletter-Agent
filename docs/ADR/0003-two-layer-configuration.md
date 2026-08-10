# ADR 0003 — Runtime settings override `.env`

**Status:** Accepted · **Date:** 2026-08-08 · **Extends:** D-19 · **Registered as:** D-24

## Context

Until M8, every configuration change meant editing `.env` and restarting. Fine for a
developer; useless for the marketing executive who is the primary user and has never
seen a terminal.

The M8 acceptance criterion was explicit: *a second account can log in, change the
LLM endpoint URL, test it, and run a campaign — without touching a file or a
terminal.*

The tension: D-19 says **secrets live only in `.env`**, and a settings page that
writes API keys to SQLite would put them in every backup of that file, in every query
output, and in whatever gets attached to a bug report.

Separately, `SettingsRepository` had existed since M1.3 with **zero production
callers** — the `settings` table was dead weight.

## Decision

**Two layers.** A saved value in the `settings` table overrides `.env`; "Revert"
restores the file value. `.env` remains the source of truth for a fresh install, so
a handover is still configured by a file the next developer reads in Git.

28 settings are runtime-editable. Four mechanisms make it safe:

1. **Secrets cannot reach the registry.** `_validate_registry()` runs at import and
   raises if any registered field is a `SecretStr`. Adding `llm.api_key` fails the
   build, not review. **D-19 becomes a mechanism instead of a convention.**
2. **`validate_assignment=True`** on the settings sections, so an edit runs the same
   validators as startup. A rejected value leaves the previous one in place — the
   app is never left holding a configuration it could not have booted with.
3. **The stored value is the normalised one**, read back off the model after
   assignment. `https://host/v1/` persists as `https://host`. Storing raw input would
   leave the database and the running process disagreeing about the endpoint — on the
   one page you would visit to diagnose that.
4. **Live mutation, not a rebuild.** The settings object is mutated in place, so
   every existing holder sees the change.

## Consequences

Good:
- The M8 criterion is met, verified end to end
- `.env` still configures a fresh install, so the handover story is unchanged
- A saved value that no longer validates is logged and skipped at startup, never
  fatal — refusing to boot over a value only fixable in the UI you just prevented
  from loading is a trap with no exit

Bad:
- **"What is the current configuration?" now has two answers.** Mitigated by showing
  provenance (`.env` vs saved) on every field, but it is genuinely more to reason
  about than one file
- **Live mutation depends on an invariant of other modules**: the factories call
  `get_settings()` per operation rather than caching a provider. If someone later
  caches one, settings changes silently stop taking effect. `TestChangesReachTheRestOfTheApp`
  asserts this so it fails loudly instead
- `configure_logging` is one-shot, so the log-level setting needed its own
  `set_log_level()` path. Without it the UI would have reported success and changed
  nothing — a silent lie, worse than refusing the edit
