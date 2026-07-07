# Onboarding a specialist's machine (Cowork-only, one account)

This covers handing our custom Meta MCP server (reads + guarded writes) to **someone else**, on
**their own machine**, working entirely through **Claude Desktop/Cowork chat** — no repo checkout
knowledge, no day-to-day CLI use, and scoped to **exactly one ad account**.

This is a manual, single-specialist process. It is *not* the production shape — see
[Where this goes next](#where-this-goes-next) for the role-based, multi-specialist version that's
already scoped as backlog work.

## What this is not

- **Not** multi-user auth. There is one Meta token and one approval secret *per machine*, not per
  role. The specialist's approval secret only proves "a human on this machine confirmed this
  write" — same trust model as the operator's own local setup, just a second, independent
  instance of it.
- **Not** a way to give someone access to *your* accounts. This process scopes their machine's
  `config/meta_ads_accounts.json` to a single account on purpose (see step 2 below) — it is the
  concrete floor implied by the earlier decision that a specialist's machine should only reach
  the one account they run.

## Before you start (the operator's steps — not scripted, deliberately)

1. **Repo access.** This repo is private. Add the specialist as a GitHub collaborator (or hand
   them a copy some other way) — a deliberate, visible action you take yourself; nothing here
   automates it.
2. **A scoped Meta access token.** Get an `ads_management`-scoped token that only has access to
   the *one* ad account this specialist runs — see
   [META_API_SETUP.md → Getting an access token](META_API_SETUP.md#getting-an-access-token) for
   the actual steps (use the System User path, not a personal Graph API Explorer token, for
   anyone other than yourself). Hand it over through a secure channel — never plaintext chat,
   email, or a ticket; a password-manager share link works and doesn't require them to have an
   account with that service.
3. **That one account's config block.** From your own (gitignored, local-only)
   `config/meta_ads_accounts.json`, copy out **just that one account's `{...}` object** — not your
   whole file — into a standalone JSON file. That's what `scripts/onboard_specialist.sh` expects
   as input in step 2 below. Send it to the specialist the same secure way as the token.

## What the specialist does, on their own machine

1. Clone the repo (needs the GitHub access from step 1 above) and open a terminal in it.
2. Run the setup script:
   ```bash
   scripts/onboard_specialist.sh
   ```
   It will:
   - Create a `.venv` and install the project + server extra.
   - Ask for the path to the single-account JSON file from step 3 above, and write a
     `config/meta_ads_accounts.json` containing **only that one account** (the same gitignore
     rule that protects the operator's real config protects theirs too).
   - Generate their **own** HMAC approval secret under `local/approval_secret` (gitignored,
     `chmod 600`, never shared with — or by — the operator).
   - Prompt for the Meta access token from step 2 above and write it into their own `.env`.
   - Write a personal `local/approve.sh` wrapper (gitignored) that signs plans with their secret.
3. Test in **mock mode** first — the script prints the exact command. Confirm the server starts
   and stop it (Ctrl-C) before wiring up Desktop.
4. Add the MCP server entry the script prints to Claude Desktop (**Settings → Developer → Edit
   Config**), then fully quit and reopen Desktop. Their account's read/write tools appear in chat.
5. Day to day: ask Cowork to read/analyze freely. For a write, Cowork will produce a `plan_id` —
   run `local/approve.sh --plan-id <id> --all` in a terminal to sign it, then ask Cowork to
   execute. This is the same propose → approve → validate → execute → verify loop the operator
   uses locally; only the approver's identity differs.

## Rotating or revoking access

- **Rotate their secret:** delete `local/approval_secret` on their machine and re-run the script
  (or just regenerate it manually) — old signed-but-unexecuted plans become unapprovable, which is
  the intended fail-safe.
- **Revoke entirely:** remove their GitHub collaborator access and revoke/expire their Meta token
  in Business Manager. There's no server-side session to invalidate in this local setup — the
  token and the repo checkout are the only two things that grant access.

## Where this goes next

This whole process is a manual stand-in for work already scoped in `tickets/backlog/`:
- `mcp-role-based-access-tiers` — real per-person auth (Entra ID) instead of one-token-per-machine,
  and server-side approval state a specialist can't forge their own way around.
- `mcp-azure-knowledge-store` — knowledge (learnings, decision logs) served through the MCP server
  itself instead of living as git-tracked files, so a new specialist's machine never needs a full
  repo clone (and can't accidentally end up with data from every other account, the way Divine
  Designs' data ended up public before it was caught and fixed — see that ticket for the incident).
- `mcp-cowork-distribution` — the hosted, org-wide version of what this doc does by hand for one
  person at a time.

Reach for those once onboarding more than one or two specialists this way starts to hurt.
