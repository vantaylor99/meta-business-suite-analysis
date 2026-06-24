/**
 * Per-ticket stage runner.
 *
 * Encapsulates one stage transition for one ticket: stop check, agent
 * invocation with idle-timeout retries, in-progress state, log file,
 * and commit.  All strategies share this helper; the only thing they
 * decide is which ticket to feed it next.
 *
 * Outcome kinds:
 *   - 'success'      : agent completed cleanly; ticket was advanced
 *   - 'timed-out'    : exhausted retries; resume note prepended; ticket
 *                      remains in its source stage
 *   - 'agent-error'  : agent exited non-zero (non-timeout); the strategy
 *                      decides whether to abort the run.  `exitCode` is set.
 *   - 'skipped'      : ticket file was already moved before we ran it
 *   - 'stopped'      : .stop file detected; no work performed
 *   - 'deferred'     : a cross-stage prereq is still behind (or parked in
 *                      blocked/); the strategy adds the slug to its run-local
 *                      deferred set so dependents cascade
 */

import { writeFile, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { execSync } from 'node:child_process';
import { NEXT_STAGE, formatSeq, findUnsatisfiedPrereq } from './tickets.mjs';
import { runAgent, MAX_TIMEOUT_RETRIES } from './process.mjs';
import { commitTicket } from './git.mjs';
import { writeInProgress, clearInProgress, addResumeNote, checkStop } from './state.mjs';
import { logPath } from './logging.mjs';
import { buildPrompt } from './prompt.mjs';
import { maybeRefreshIndex } from './refresh-index.mjs';
import { handlePreExistingError } from './pre-existing-error.mjs';

/**
 * Persist a resume note onto a ticket that did not complete cleanly, and
 * commit it so the next run picks up where this one left off.  `.in-progress`
 * is git-ignored, so the committed note — not the marker — is what carries
 * resume state across commits and checkouts.  Shared by the timed-out and
 * agent-error paths; both leave the ticket in its source stage.
 */
async function persistResumeNote(ticket, ctx, { startedAt, logFile, commitVerb }) {
	const { repoRoot, opts } = ctx;
	try {
		await access(ticket.path, constants.R_OK);
		await addResumeNote(ticket.path, { startedAt, agent: opts.agent, logFile });
		if (!opts.noCommit) {
			try {
				execSync('git add -A', { cwd: repoRoot, encoding: 'utf-8' });
				execSync(`git commit -m "tess: ${commitVerb} on ${ticket.slug} — added resume note"`, { cwd: repoRoot, encoding: 'utf-8' });
			} catch (err) {
				console.warn(`    Failed to commit resume note: ${err.message}`);
			}
		}
	} catch { /* ticket file may have been moved */ }
}

export async function runOneStage(ticket, ctx, { label }) {
	const { ticketsDir, repoRoot, tessRoot, tessVersion, logsDir, opts } = ctx;

	if (await checkStop(ticketsDir)) {
		console.log('\n⏹  Stop file detected — halting before next ticket.');
		return { kind: 'stopped' };
	}

	// Guard: a previous agent may have already moved this ticket.
	try {
		await access(ticket.path, constants.R_OK);
	} catch {
		console.log(`\n  ${label} Skipped (already moved): ${ticket.file}\n`);
		return { kind: 'skipped' };
	}

	if (opts.refreshIndex) {
		await maybeRefreshIndex(repoRoot);
	}

	// Cross-stage prereq gate: if a prereq lives in an earlier-rank stage,
	// a peer-but-different stage, or blocked/, defer this ticket.  Same-stage
	// edges are handled by the per-stage topo sort and pass through here.
	const unsatisfied = await findUnsatisfiedPrereq(ticket, ticketsDir);
	if (unsatisfied) {
		console.log(`\n  ${label} Deferred ${ticket.file}: prereq "${unsatisfied.slug}" is in ${unsatisfied.stage}/.\n`);
		return { kind: 'deferred', prereq: unsatisfied.slug, prereqStage: unsatisfied.stage };
	}

	let attempt = 0;
	let lastResult = null;
	let lastLogFile = null;
	let lastStartedAt = null;
	let success = false;

	while (attempt <= MAX_TIMEOUT_RETRIES) {
		// On retry, prepend a resume note pointing at the prior attempt's log so
		// the agent can read what it had been doing and resume rather than restart.
		if (attempt > 0) {
			try {
				await access(ticket.path, constants.R_OK);
			} catch {
				console.log(`  Ticket no longer present — not retrying.`);
				break;
			}
			try {
				await addResumeNote(ticket.path, {
					startedAt: lastStartedAt,
					agent: opts.agent,
					logFile: lastLogFile,
				});
				console.log(`\n  Retrying after timeout (attempt ${attempt + 1}/${MAX_TIMEOUT_RETRIES + 1}) — resume note added.`);
			} catch (err) {
				console.warn(`  Failed to add resume note: ${err.message}`);
			}
			if (await checkStop(ticketsDir)) {
				console.log('\n⏹  Stop file detected — halting before retry.');
				return { kind: 'stopped' };
			}
		}

		const currentLog = logPath(logsDir, ticket);
		const startedAt = new Date().toISOString();
		lastLogFile = currentLog;
		lastStartedAt = startedAt;

		const attemptLabel = attempt > 0 ? `  (retry ${attempt})` : '';
		const ticketBanner = [
			`${'─'.repeat(72)}`,
			`  ${label} ${ticket.file}${attemptLabel}`,
			`  Stage: ${ticket.stage} → ${NEXT_STAGE[ticket.stage]}  |  Sequence: ${formatSeq(ticket.sequence)}`,
			`  Log: ${currentLog}`,
			`${'─'.repeat(72)}`,
		].join('\n');
		console.log(ticketBanner);

		await writeFile(currentLog, [
			`Ticket: ${ticket.file}`,
			`Stage: ${ticket.stage} → ${NEXT_STAGE[ticket.stage]}`,
			`Sequence: ${formatSeq(ticket.sequence)}`,
			`Agent: ${opts.agent}`,
			`Tess: ${tessVersion}`,
			`Started: ${startedAt}`,
			`Attempt: ${attempt + 1}${attempt > 0 ? ' (retry after timeout)' : ''}`,
			'═'.repeat(72),
			'',
		].join('\n'));

		await writeInProgress(ticketsDir, ticket, currentLog, opts.agent);

		let prompt;
		try {
			prompt = await buildPrompt(ticket, tessRoot, repoRoot);
		} catch (err) {
			if (err.code === 'ENOENT') {
				await clearInProgress(ticketsDir);
				console.log(`\n  ${label} Skipped (removed mid-processing): ${ticket.file}\n`);
				return { kind: 'skipped' };
			}
			throw err;
		}
		lastResult = await runAgent(opts.agent, prompt, repoRoot, currentLog, {
			stage: ticket.stage,
			tokenBudget: opts.tokenBudget,
			difficulty: ticket.difficulty,
		});

		if (lastResult.exitCode === 0) {
			success = true;
			break;
		}

		if (lastResult.timedOut && attempt < MAX_TIMEOUT_RETRIES) {
			console.error(`\n  Ticket timed out — will retry with resume note.`);
			attempt++;
			continue;
		}

		break;
	}

	if (success) {
		await clearInProgress(ticketsDir);
		if (!opts.noCommit && commitTicket(ticket, repoRoot)) {
			console.log(`  Committed.`);
		}
		await handlePreExistingError(ctx);
		console.log(`\n  ${label} Complete: ${ticket.file}\n`);
		return { kind: 'success', budgetTriggered: !!lastResult?.budgetTriggered };
	}

	if (lastResult?.timedOut) {
		// All timeout retries exhausted. Annotate the ticket with a resume note
		// pointing at the latest log so the next run picks up where this one
		// left off, then return so the strategy can decide what to do next
		// (batch continues; chase ends the chain).
		await persistResumeNote(ticket, ctx, { startedAt: lastStartedAt, logFile: lastLogFile, commitVerb: 'timed out' });
		await clearInProgress(ticketsDir);
		console.error(`\n  ${label} Timed out ${attempt + 1} time(s) on: ${ticket.file}`);
		console.error(`    Latest log: ${lastLogFile}`);
		console.error(`    Resume note added — re-run tess to pick up where it left off.\n`);
		return { kind: 'timed-out' };
	}

	// Agent exited non-zero (non-timeout). Preserve a resume note for the FIRST
	// errored ticket of the run so the next run resumes its partial work. A
	// credit outage fails every remaining ticket, but only the first did any
	// real work, so later errors are left alone — annotating them would only add
	// noise (and, before this gate, each would overwrite `.in-progress`, leaving
	// the marker on the LAST ticket rather than the first). `ctx` is the same
	// object for every ticket in a run and is recreated per run, so the flag
	// scopes to "first error this run". The committed note — not the git-ignored
	// `.in-progress` marker — is what carries resume state forward, so we clear
	// the marker here as the success and timed-out paths do.
	if (!ctx.firstAgentErrorNoted) {
		ctx.firstAgentErrorNoted = true;
		await persistResumeNote(ticket, ctx, { startedAt: lastStartedAt, logFile: lastLogFile, commitVerb: 'agent error' });
	}
	await clearInProgress(ticketsDir);

	if (lastResult) {
		console.error(`\nAgent exited with code ${lastResult.exitCode} on ticket: ${ticket.file}`);
		console.error(`Log: ${lastLogFile}`);
		return { kind: 'agent-error', exitCode: lastResult.exitCode };
	}

	return { kind: 'agent-error', exitCode: 1 };
}
