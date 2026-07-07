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

## If the specialist has no terminal/CLI comfort at all

Everything below assumes they can type answers into a terminal window when prompted, but never
need to *know* shell commands. That works because `scripts/onboard_specialist.sh` is interactive
(it asks questions) and there's a double-clickable launcher for it — no command line to type or
memorize. If they genuinely can't run **any** one-time setup themselves (not even double-clicking
a file and answering its prompts), do the whole "What the specialist does" section yourself, in
person or over a screen share, instead of handing it off.

## Before you start (the operator's steps — not scripted, deliberately)

1. **Get the repo onto their machine.** This repo is private, so `git clone` needs your GitHub
   access — do the clone yourself (in person or over a screen share) rather than trying to get
   them GitHub access and teaching them git. Cloning is the only git step involved; everything
   after this is double-click + answer-the-prompt.
2. **A scoped Meta access token.** Get an `ads_management`-scoped token that only has access to
   the *one* ad account this specialist runs — see
   [META_API_SETUP.md → Getting an access token](META_API_SETUP.md#getting-an-access-token) for
   the actual steps (use the System User path, not a personal Graph API Explorer token, for
   anyone other than yourself). Hand it over through a secure channel — never plaintext chat,
   email, or a ticket; a password-manager share link works and doesn't require them to have an
   account with that service.
3. **That one account's config block.** From your own (gitignored, local-only)
   `config/meta_ads_accounts.json`, copy out **just that one account's `{...}` object** — not your
   whole file — into a standalone JSON file, and save/send it somewhere they can find it (e.g. on
   their Desktop) — they'll need its path in step 2 below. Send it the same secure way as the token.

## What the specialist does, on their own machine

1. Double-click **`scripts/Setup Meta Ads Account.command`** in Finder. (First time only, macOS
   may say it's from an unidentified developer — right-click it → **Open** → confirm once; that
   shouldn't come up for a file that arrived via `git clone` rather than a browser download, but
   macOS is occasionally inconsistent about it.) A Terminal window opens and walks through the
   rest — nothing here needs typing a command, only answering what it asks.
2. When it asks for the path to the single-account JSON file (step 3 above): rather than typing
   the path, **drag that file from Finder into the Terminal window** — it fills in the full path
   automatically — then press Enter.
3. When it asks for the Meta access token: paste it in (the window hides what's typed) and press
   Enter.
4. It prints a block to paste into Claude Desktop's config, and a reminder to test in mock mode
   first. Follow along in the same window; it tells you exactly what to do next, including the
   **Settings → Developer → Edit Config** step in Claude Desktop.
5. Fully quit and reopen Claude Desktop after adding that config. Their account's read/write tools
   appear in chat.
6. Day to day: ask Cowork to read/analyze freely. For a write, Cowork will produce a `plan_id` —
   double-click **`local/Approve.app`**. A normal macOS popup appears (no terminal window) asking
   for the plan_id — paste it in and click **Approve**, then go back to Cowork/Desktop and ask it
   to execute. This is the same propose → approve → validate → execute → verify loop the operator
   uses locally; only the approver's identity differs.
   - If `local/Approve.app` doesn't work (some managed/corporate Macs restrict AppleScript running
     shell commands), use `local/Approve.command` instead — same idea, opens a terminal window.

## Rotating or revoking access

- **Rotate their secret:** delete `local/approval_secret` on their machine and re-run the script
  (or just regenerate it manually) — old signed-but-unexecuted plans become unapprovable, which is
  the intended fail-safe.
- **Revoke entirely:** revoke/expire their Meta token in Business Manager (for a System User
  token: delete the System User or remove its ad-account asset assignment) and, if they do have
  their own GitHub access, remove it. There's no server-side session to invalidate in this local
  setup — the token and the repo checkout are the only two things that grant access.

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
