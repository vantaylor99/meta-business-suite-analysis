/**
 * Pre-existing test-failure triage.
 *
 * When an agent working a normal ticket runs tests and hits a failure it
 * judges to be unrelated to its own changes, the workflow rules tell it to
 * drop a short report into `tickets/.pre-existing-error.md` and continue.
 * After every ticket commits (and again once the run finishes), the runner
 * calls `handlePreExistingError`: it picks up the report, invokes a triage
 * agent against it, removes the file, and commits whatever the triage
 * produced (a fix in-place or a new `tickets/backlog/` ticket).
 *
 * The triage agent uses the same adapter (claude/cursor/etc.) as the rest
 * of the pipeline but with a focused prompt — no per-stage rules, no MCP
 * directives — because the report supplies the only context it needs.
 */

import { readFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { execSync } from 'node:child_process';
import { runAgent } from './process.mjs';

const REPORT_FILE = '.pre-existing-error.md';

function reportPath(ticketsDir) {
	return join(ticketsDir, REPORT_FILE);
}

async function readReport(ticketsDir) {
	try {
		return await readFile(reportPath(ticketsDir), 'utf-8');
	} catch {
		return null;
	}
}

function buildTriagePrompt(report) {
	return [
		'# Triage: pre-existing test failure',
		'',
		'A prior tess agent, while working an unrelated ticket, encountered a test',
		'failure it judged to be pre-existing (not caused by its own changes) and',
		'wrote the report below. Your job is to triage it:',
		'',
		'  1. Re-run the indicated test(s) and confirm the failure reproduces at HEAD.',
		'  2. If you can identify and fix the root cause with reasonable confidence,',
		'     do so. Keep the fix tightly scoped — do not refactor unrelated code.',
		'     The runner will commit your changes after you exit.',
		'  3. If a confident fix is not in reach, create a new ticket in',
		'     `tickets/backlog/` (filename `<slug>.md`, no sequence prefix) using',
		'     the standard tess header (description/files) followed by a body that',
		'     captures the failing test, the error output, and what you ruled out.',
		'',
		'Do NOT modify or re-write `tickets/.pre-existing-error.md` — the runner',
		'deletes it after you exit. Do NOT commit; the runner handles commits.',
		'Do NOT advance, touch, or create tickets outside `backlog/`.',
		'Do NOT run `git checkout -- `, `git restore`, `git reset`, `git clean`, or',
		'`git stash`, and do not otherwise revert or discard working-tree changes you',
		'did not make. The tree may carry concurrent edits — board promotions, other',
		'in-flight work — that are not yours to undo. Reproduce and fix the failure in',
		'place; "at HEAD" means the current committed state, not a sanitized tree.',
		'',
		'## Report',
		'',
		report,
	].join('\n');
}

/**
 * If a pre-existing-error report is present, dispatch a triage agent against
 * it, remove the file, and commit any resulting changes. Returns true if a
 * triage pass was attempted.
 */
export async function handlePreExistingError(ctx) {
	const { ticketsDir, repoRoot, logsDir, opts } = ctx;
	const report = await readReport(ticketsDir);
	if (!report) return false;

	const ts = new Date().toISOString().replace(/[:.]/g, '-');
	const logFile = join(logsDir, `pre-existing-error.${ts}.log`);
	console.log(`\n  ⚠  Pre-existing test failure reported — dispatching triage agent.`);
	console.log(`     Log: ${logFile}`);

	const prompt = buildTriagePrompt(report);
	try {
		const result = await runAgent(opts.agent, prompt, repoRoot, logFile, {
			stage: 'triage',
			tokenBudget: opts.tokenBudget,
		});
		if (result.exitCode !== 0) {
			const suffix = result.timedOut ? ' (idle timeout)' : '';
			console.warn(`     Triage agent exited ${result.exitCode}${suffix}.`);
		}
	} catch (err) {
		console.warn(`     Triage agent failed to spawn: ${err.message}`);
	}

	// Always remove the report so the loop terminates even if the agent left it.
	await unlink(reportPath(ticketsDir)).catch(() => {});

	if (!opts.noCommit) {
		try {
			const status = execSync('git status --porcelain', { cwd: repoRoot, encoding: 'utf-8' }).trim();
			if (status) {
				execSync('git add -A', { cwd: repoRoot, encoding: 'utf-8' });
				execSync('git commit -m "tess: triage pre-existing test failure"', { cwd: repoRoot, encoding: 'utf-8' });
				console.log('     Committed triage result.');
			}
		} catch (err) {
			console.warn(`     Triage commit failed: ${err.message}`);
		}
	}
	return true;
}
