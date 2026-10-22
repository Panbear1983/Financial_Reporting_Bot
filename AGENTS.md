# Financial_Reporting_Bot - agent rules

**Memory wing: `financial_reporting_bot`.** All agents (Claude Code, Hermes, Codex, Antigravity) share ONE long-term
memory, the mempalace, reached through the `mempalace_*` tools. Full rules and wing table:
`~/.mempalace/MEMORY_PROTOCOL.md`.

Peter's whole-brokerage wealth report (Taiwan stocks + cash): 09:05 morning push, month-end total-wealth picture.

## Every task in this repo
1. Start: `mempalace_search(<what you are about to do>, wing="financial_reporting_bot")` before planning.
2. Finish: one `mempalace_add_drawer(wing="financial_reporting_bot", room=<decisions|backend|bugs|reference>)`:
   what was done, what was verified, what is still open, Peter's words verbatim where they matter.
   Dated facts (bug fixed, decision made, thing deployed) also go to `mempalace_kg_add`.
3. Never file secrets, tokens or financial figures.
4. The Hermes kanban board is live work state, not memory; do not copy cards into the palace.

## Standing rules for this repo
- dashboard.py is the single data layer; the TUI is run_tui.sh. start.sh is DEAD - never chmod or run it (it doubles every push).
- Cash is never inside a P/L figure. Red = up (Taiwan colour rule) on every surface.
- Peter's cash ledger / financial records never go online, not even a private repo.
