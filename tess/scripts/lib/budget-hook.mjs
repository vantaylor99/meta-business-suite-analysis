#!/usr/bin/env node
/**
 * PreToolUse hook that injects a BUDGET_WARNING into Claude's context when
 * the runner has flagged the session as over its soft token budget.
 *
 * The runner writes the warning text to the file named by env var
 * TESS_BUDGET_FLAG_FILE; this hook reads it on every PreToolUse, injects it
 * once via `additionalContext`, and removes the file so the warning is not
 * re-injected on every subsequent tool call.
 *
 * Stays silent (zero output, exit 0) when no flag file is present.
 */

import { existsSync, readFileSync, unlinkSync } from 'node:fs';

const flagFile = process.env.TESS_BUDGET_FLAG_FILE;
if (!flagFile || !existsSync(flagFile)) process.exit(0);

let context;
try {
	context = readFileSync(flagFile, 'utf-8').trim();
} catch {
	process.exit(0);
}
if (!context) process.exit(0);

try { unlinkSync(flagFile); } catch { /* race with another hook fire is fine */ }

const out = {
	hookSpecificOutput: {
		hookEventName: 'PreToolUse',
		additionalContext: context,
	},
};
process.stdout.write(JSON.stringify(out));
