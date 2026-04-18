# Sentinel Project

The sentinel project handles ratbag events from connected peripherals and dispatches
them to subscribers via the in-process bus.

It exists purely as a deterministic fixture for the gitdoc integration test
suite — every distinctive phrase below is sourced from exactly one file so a
citation test can verify the orchestrator returns the right path.

## Layout

- `src/calculator.py` — arithmetic helpers (the only place `add` is defined).
- `src/eventbus.py` — pub/sub primitive used by the ratbag pipeline.
- `docs/architecture.md` — high-level design decisions.
- `docs/glossary.md` — definitions of project-specific jargon.
