#!/usr/bin/env bash
# One-time setup for a NEW specialist machine: this account manager will work through Claude
# Cowork/Desktop only (no CLI day-to-day), connected to our custom MCP server (reads + guarded
# writes) scoped to exactly ONE ad account. Run this ONCE, on the specialist's own machine, from
# a checkout of this (private) repo.
#
# What this does NOT do (deliberately, out of scope for a script):
#   - It does not grant GitHub access to this repo. The operator (you) adds the specialist as a
#     collaborator on the private repo separately.
#   - It does not obtain a Meta access token. The operator gets one (scoped to the ONE ad account
#     this specialist manages) via Meta Business Manager and hands it over through a secure
#     channel — never paste it into chat, email, or a ticket.
#   - It does not touch config/meta_ads_accounts.json for any OTHER account. This script only
#     ever writes a single-account registry, on purpose — see the account-scope section below.
#
# See docs/SPECIALIST_ONBOARDING.md for the full walkthrough this script automates.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCAL_DIR="$REPO_ROOT/local"
mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"

echo "== Meta MCP server: specialist machine setup =="
echo "Repo: $REPO_ROOT"
echo

# --- 1. Python environment -------------------------------------------------
if [ ! -d "$REPO_ROOT/.venv" ]; then
  echo "-- Creating .venv"
  python3 -m venv "$REPO_ROOT/.venv"
fi
echo "-- Installing project + server extra into .venv (this can take a minute)"
"$REPO_ROOT/.venv/bin/pip" install -q -e ".[server]"

# --- 2. Scoped account config (exactly ONE account) ------------------------
ACCOUNTS_PATH="$REPO_ROOT/config/meta_ads_accounts.json"
if [ -f "$ACCOUNTS_PATH" ]; then
  echo
  echo "config/meta_ads_accounts.json already exists — leaving it as-is."
  echo "(Delete it first if you want this script to rewrite it from a fresh account file.)"
else
  echo
  read -r -p "Path to the single-account JSON block your operator gave you (one {...} object, not the full registry): " ACCOUNT_JSON_PATH
  if [ ! -f "$ACCOUNT_JSON_PATH" ]; then
    echo "No file found at $ACCOUNT_JSON_PATH — aborting. Re-run once you have it." >&2
    exit 1
  fi
  "$REPO_ROOT/.venv/bin/python" - "$ACCOUNT_JSON_PATH" "$ACCOUNTS_PATH" <<'PYEOF'
import json
import sys

src_path, dest_path = sys.argv[1], sys.argv[2]
account = json.loads(open(src_path, encoding="utf-8").read())
if isinstance(account, dict) and "accounts" in account:
    # Tolerate being handed a full {"accounts": [...]} registry with exactly one entry.
    accounts = account["accounts"]
    if len(accounts) != 1:
        raise SystemExit(
            f"Expected exactly one account in {src_path}, found {len(accounts)}. "
            "This machine is scoped to a single account on purpose."
        )
    account = accounts[0]
with open(dest_path, "w", encoding="utf-8") as f:
    json.dump({"accounts": [account]}, f, indent=2)
    f.write("\n")
print(f"Wrote scoped registry ({account.get('account_slug')!r}) -> {dest_path}")
PYEOF
fi

# --- 3. Approval secret (this specialist's own — never Van's) --------------
SECRET_PATH="$LOCAL_DIR/approval_secret"
if [ -f "$SECRET_PATH" ]; then
  echo
  echo "$SECRET_PATH already exists — reusing it (delete it first to rotate)."
else
  echo
  echo "-- Generating your own HMAC approval secret (kept out of git, out of Van's secret)"
  "$REPO_ROOT/.venv/bin/python" -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_PATH"
  chmod 600 "$SECRET_PATH"
fi
SECRET_VALUE="$(cat "$SECRET_PATH")"

# --- 4. Meta access token ----------------------------------------------------
ENV_PATH="$REPO_ROOT/.env"
if [ -f "$ENV_PATH" ] && grep -q "^META_ACCESS_TOKEN=" "$ENV_PATH" 2>/dev/null; then
  echo
  echo ".env already has META_ACCESS_TOKEN — leaving it as-is."
else
  echo
  read -r -s -p "Paste the Meta access token your operator gave you (input hidden): " META_TOKEN
  echo
  printf 'META_ACCESS_TOKEN=%s\n' "$META_TOKEN" >> "$ENV_PATH"
  chmod 600 "$ENV_PATH"
  echo "Wrote META_ACCESS_TOKEN to $ENV_PATH"
fi

# --- 5. Your own approve.sh (self-approve your own proposed writes) --------
APPROVE_PATH="$LOCAL_DIR/approve.sh"
cat > "$APPROVE_PATH" <<EOF
#!/usr/bin/env bash
# Approves a proposed write plan with YOUR OWN approval secret. Run this after Cowork/Desktop
# shows you a plan_id, before asking it to execute.
set -euo pipefail
export META_APPROVAL_SECRET="\$(cat "$SECRET_PATH")"
exec "$REPO_ROOT/.venv/bin/approve_plan" "\$@"
EOF
chmod 700 "$APPROVE_PATH"

# Terminal-based double-clickable fallback — prompts for the plan id, no full command to type.
# Kept in case the native app below (Approve.app) hits a corporate Mac restriction.
APPROVE_COMMAND_PATH="$LOCAL_DIR/Approve.command"
cat > "$APPROVE_COMMAND_PATH" <<EOF
#!/usr/bin/env bash
# Double-click this to approve a write plan. Cowork will show you a "plan_id" before this is
# needed — have that ready to paste in.
read -r -p "Paste the plan_id Cowork showed you: " PLAN_ID
if "$APPROVE_PATH" --plan-id "\$PLAN_ID" --all; then
  echo
  echo "Approved. Go back to Cowork/Desktop and ask it to execute the plan."
else
  echo
  echo "Something went wrong — check the plan_id was pasted correctly and try again."
fi
echo
read -r -p "Press Enter to close this window..."
EOF
chmod 700 "$APPROVE_COMMAND_PATH"

# Native macOS app (no terminal at all) — a real popup dialog asking for the plan id, then a
# native alert with the result. This is the recommended day-to-day approval method; the .command
# above is the fallback if a managed/corporate Mac blocks AppleScript's "do shell script".
APPROVE_APP_PATH="$LOCAL_DIR/Approve.app"
if command -v osacompile >/dev/null 2>&1; then
  APPLESCRIPT_SRC="$LOCAL_DIR/.approve_dialog_src.applescript"
  cat > "$APPLESCRIPT_SRC" <<APPLESCRIPT_EOF
try
	set planID to text returned of (display dialog "Paste the plan_id Cowork showed you:" default answer "" with title "Approve Meta Ads Write" buttons {"Cancel", "Approve"} default button "Approve")
on error number -128
	return
end try

set approveScript to "$APPROVE_PATH"

try
	set cmd to (quoted form of approveScript) & " --plan-id " & (quoted form of planID) & " --all 2>&1"
	set shellResult to do shell script cmd
	display alert "Approved" message "Go back to Cowork/Desktop and ask it to execute the plan." & return & return & shellResult
on error errMsg
	display alert "Something went wrong" message errMsg as critical
end try
APPLESCRIPT_EOF
  rm -rf "$APPROVE_APP_PATH"
  osacompile -o "$APPROVE_APP_PATH" "$APPLESCRIPT_SRC"
  rm -f "$APPLESCRIPT_SRC"
else
  APPROVE_APP_PATH=""
  echo
  echo "Note: 'osacompile' wasn't found, so the native Approve.app popup couldn't be built. Use"
  echo "$APPROVE_COMMAND_PATH instead (double-click, same idea, just a terminal window)."
fi

# --- 6. Claude Desktop config snippet ---------------------------------------
echo
echo "== Done with local setup. Next steps =="
echo
echo "1. Test in mock mode first (no live Meta calls, safe to explore):"
echo "     META_APPROVAL_SECRET=\"$SECRET_VALUE\" \"$REPO_ROOT/.venv/bin/meta_mcp_server\" --stdio --mock"
echo "   (Ctrl-C to stop once you've confirmed it starts.)"
echo
echo "2. Open Claude Desktop -> Settings -> Developer -> Edit Config, and add this entry inside"
echo "   the top-level \"mcpServers\" object (create that object if it doesn't exist):"
echo
cat <<EOF
    "meta-suite-$(basename "$REPO_ROOT")": {
      "command": "$REPO_ROOT/.venv/bin/meta_mcp_server",
      "args": ["--stdio"],
      "env": {
        "META_APPROVAL_SECRET": "$SECRET_VALUE",
        "META_ACCESS_TOKEN": "<use the token you already put in .env — copy it here too>"
      }
    }
EOF
echo
echo "3. Fully quit and reopen Claude Desktop. Your account's read/write tools appear in chat."
echo
echo "4. To approve a write Cowork proposes: double-click"
if [ -n "$APPROVE_APP_PATH" ]; then
  echo "     $APPROVE_APP_PATH"
  echo "   A popup asks for the plan_id Cowork shows you — paste it in and click Approve."
  echo "   (If that app doesn't work — some managed/corporate Macs restrict AppleScript — use"
  echo "   $APPROVE_COMMAND_PATH instead, same idea via a terminal window.)"
else
  echo "     $APPROVE_COMMAND_PATH"
  echo "   and paste in the plan_id Cowork shows you when it asks."
fi
echo "   (Terminal command equivalent, if you prefer: $APPROVE_PATH --plan-id <id> --all)"
echo
echo "See docs/SPECIALIST_ONBOARDING.md if anything above doesn't match what you see."
