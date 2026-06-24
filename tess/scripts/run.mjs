#!/usr/bin/env node
/**
 * Ticket Runner — processes outstanding tickets through the pipeline stages
 * by invoking an agentic CLI tool for each one.
 *
 * Version: 2.0.0
 *
 * Key design choices:
 *   - The default `live` strategy re-discovers the ticket board after every
 *     transition, so tickets the agent creates mid-run are picked up and
 *     re-prioritized in the same run.  The `batch` and `chase` strategies instead
 *     snapshot the ticket list once at startup — tickets created during the run
 *     are NOT picked up until the next invocation, so each snapshotted ticket
 *     advances exactly one stage per run.  Either way the snapshot built below is
 *     used for the startup cycle check, the dry-run listing, the banner, and
 *     resume-note placement; `live` owns discovery from there on.
 *   - The agent owns the stage transition: it creates next-stage file(s) and
 *     deletes the source ticket file.  The runner commits after the agent completes.
 *     This keeps commits out of interactive agent sessions while ensuring clean
 *     commit-per-ticket history when running the pipeline.
 *   - Agent logs are captured in tickets/.logs/ (git-ignored), one per ticket per stage.
 *   - Numeric filename prefix encodes *sequence* (lower runs sooner); the prefix is
 *     optional — unnumbered tickets follow after all numbered ones in a stage.
 *   - Tickets may declare `prereq: <slug>, <slug>` in the header.  Prereqs must
 *     land (advance stage) before dependents; the runner topologically sorts the
 *     snapshot and errors on cycles or sequence-number violations.
 *   - If `tickets/.version` is missing or older than the current format, the runner
 *     auto-migrates legacy v1 tickets and commits the migration.
 *
 * This file is the orchestrator: it parses args, builds the snapshot, then hands
 * off to a strategy in lib/strategies/ that decides traversal order.  All other
 * concerns (discovery, agent invocation, logging, git, state) live in lib/.
 *
 * Usage:
 *   node tess/scripts/run.mjs [options]
 *
 * See `--help` for full options.
 */

import { mkdir } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { discoverTickets, formatSeq, indexAllTickets, findUnsatisfiedPrereq, findTransitiveBlocker, KNOWN_STAGES } from './lib/tickets.mjs';
import { topoSortAndCheck } from './lib/topo.mjs';
import { readAndClearInProgress, addResumeNote } from './lib/state.mjs';
import { ensureLogsDir, pruneOldLogs } from './lib/logging.mjs';
import { getTessVersion, runMigrationIfNeeded } from './lib/git.mjs';
import { parseArgs, formatStageSummary } from './lib/cli.mjs';
import { strategies } from './lib/strategies/index.mjs';
import { handlePreExistingError } from './lib/pre-existing-error.mjs';
import { pruneCompletedTickets } from './lib/prune-completed.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const TESS_ROOT = join(__dirname, '..');

async function main() {
	const opts = parseArgs(process.argv.slice(2));

	const repoRoot = process.cwd();
	const ticketsDir = join(repoRoot, 'tickets');
	const tessVersion = getTessVersion(TESS_ROOT);

	// Auto-migrate legacy format before snapshotting tickets.
	await runMigrationIfNeeded(ticketsDir, repoRoot, { noCommit: opts.noCommit, dryRun: opts.dryRun });

	// Sweep stale completed tickets (default: older than 30 days by git landing date).
	if (opts.pruneCompleted) {
		const pruned = await pruneCompletedTickets(ticketsDir, repoRoot, {
			maxAgeDays: opts.pruneCompletedDays,
			dryRun: opts.dryRun,
			noCommit: opts.noCommit,
		});
		if (pruned.removed > 0) {
			const verb = opts.dryRun ? 'Would prune' : 'Pruned';
			console.log(`\n  ${verb} ${pruned.removed} completed ticket(s) older than ${opts.pruneCompletedDays} days.`);
		}
	}

	// ── Build the snapshot ──
	// Discover each requested stage, then topologically sort within the stage so
	// prereqs run before dependents.  Across stages we preserve the order declared
	// via --stages.
	const allTickets = [];
	for (const { stage, maxSequence } of opts.stages) {
		const tickets = await discoverTickets(ticketsDir, stage, maxSequence);
		allTickets.push(...tickets);
	}

	if (allTickets.length === 0) {
		console.log(`No tickets found in stages: ${formatStageSummary(opts.stages)}`);
		return;
	}

	const byStage = new Map();
	for (const t of allTickets) {
		if (!byStage.has(t.stage)) byStage.set(t.stage, []);
		byStage.get(t.stage).push(t);
	}
	const ordered = [];
	for (const { stage } of opts.stages) {
		const bucket = byStage.get(stage);
		if (!bucket) continue;
		try {
			ordered.push(...topoSortAndCheck(bucket));
		} catch (err) {
			console.error(`\n[runner] ${err.message}`);
			process.exit(1);
		}
	}
	allTickets.length = 0;
	allTickets.push(...ordered);

	// --skip-blocked: pre-filter the snapshot by walking each ticket's prereq
	// chain across the cross-stage index.  Anything reaching a slug parked in
	// blocked/ is dropped before the run starts (vs the runtime gate, which
	// only defers tickets whose direct prereq is behind).
	if (opts.skipBlocked) {
		const indexWithPrereqs = await indexAllTickets(ticketsDir, { withPrereqs: true });
		const kept = [];
		const skipped = [];
		for (const t of allTickets) {
			const blocker = findTransitiveBlocker(t, indexWithPrereqs);
			if (blocker) skipped.push({ ticket: t, blocker });
			else kept.push(t);
		}
		if (skipped.length > 0) {
			console.log(`\nSkipping ${skipped.length} ticket(s) transitively blocked:`);
			for (const { ticket, blocker } of skipped) {
				console.log(`  [${ticket.stage.padEnd(9)}] ${ticket.file}  → blocked via "${blocker.slug}"`);
			}
		}
		allTickets.length = 0;
		allTickets.push(...kept);
	}

	const totalFound = allTickets.length;
	if (opts.maxTickets < totalFound) allTickets.splice(opts.maxTickets);

	if (opts.dryRun) {
		console.log(`\ntess (${tessVersion})`);
		console.log(`Pending tickets in: ${formatStageSummary(opts.stages)}`);
		console.log(`Strategy: ${opts.strategy}\n`);
		// Snapshot-time cross-stage prereq check, for visibility only — the
		// actual deferral happens at runtime against the live filesystem.
		const ticketIndex = await indexAllTickets(ticketsDir);
		for (const t of allTickets) {
			const unsat = await findUnsatisfiedPrereq(t, ticketsDir, ticketIndex);
			const note = unsat ? `  ⚠ deferred: prereq "${unsat.slug}" in ${unsat.stage}/` : '';
			console.log(`  [${t.stage.padEnd(9)}] seq ${formatSeq(t.sequence).padStart(4)}  ${t.file}${note}`);
		}
		const limitNote = totalFound > allTickets.length ? ` (limited to ${allTickets.length} of ${totalFound})` : '';
		console.log(`\n${allTickets.length} ticket(s) would be processed${limitNote}.`);
		return;
	}

	// ── Resume handling: if a prior run was interrupted, the in-progress marker
	// names the ticket that was being processed.  If it's still in this snapshot,
	// prepend a resume note so the agent picks up where it left off, and hoist
	// the ticket to the front of the queue so the resumed work runs first
	// regardless of its stage. ──
	const priorRun = await readAndClearInProgress(ticketsDir);
	if (priorRun) {
		console.log(`\n  Prior incomplete run detected: ${priorRun.file} (${priorRun.stage})`);
		console.log(`    Started: ${priorRun.startedAt}  |  Log: ${priorRun.logFile}`);
		const matchIdx = allTickets.findIndex(t => t.file === priorRun.file && t.stage === priorRun.stage);
		if (matchIdx !== -1) {
			const match = allTickets[matchIdx];
			try {
				await addResumeNote(match.path, priorRun);
				console.log(`    Added resume note to ${match.file}`);
			} catch (err) {
				console.warn(`    Failed to add resume note: ${err.message}`);
			}
			if (matchIdx > 0) {
				allTickets.splice(matchIdx, 1);
				allTickets.unshift(match);
				console.log(`    Hoisted to front of queue — resumed ticket runs first.`);
			}
		} else {
			console.log(`    Ticket no longer in batch — skipping resume note.`);
		}
	}

	const limitNote = totalFound > allTickets.length ? `, limited to ${allTickets.length}` : '';
	const banner = [
		`${'═'.repeat(72)}`,
		`  tess (${tessVersion})`,
		`  Snapshotted ${totalFound} ticket(s)${limitNote}.`,
		`  Strategy: ${opts.strategy}`,
		`${'═'.repeat(72)}`,
	].join('\n');
	console.log(banner);

	const logsDir = await ensureLogsDir(ticketsDir);
	const pruned = await pruneOldLogs(logsDir, priorRun?.logFile);
	if (pruned.removedGroups > 0) {
		console.log(`  Pruned ${pruned.removedGroups} old log set(s) (${pruned.removedFiles} file(s)).`);
	}

	const strategy = strategies[opts.strategy];
	const triageCtx = { ticketsDir, repoRoot, logsDir, opts };
	let result;
	try {
		result = await strategy.run({
			snapshot: allTickets,
			ticketsDir,
			repoRoot,
			tessRoot: TESS_ROOT,
			tessVersion,
			logsDir,
			opts,
		});
	} finally {
		// Agents sometimes rmdir a stage folder after deleting its last ticket.
		// Re-create the standard set so the next run / human sees a stable layout.
		await Promise.all(
			KNOWN_STAGES.map(s => mkdir(join(ticketsDir, s), { recursive: true })),
		);
		// Per-ticket triage runs inside run-ticket.mjs catch the common case.
		// This final sweep catches reports left when the last ticket errored or
		// timed out — i.e. the runner is about to conclude with a report still
		// sitting in tickets/.
		await handlePreExistingError(triageCtx);
	}

	const errors = result?.errors ?? [];
	if (errors.length > 0) {
		console.error(`\nDone with ${errors.length} agent error(s):`);
		for (const e of errors) {
			console.error(`  - ${e.slug} (exit ${e.exitCode})`);
		}
		process.exit(errors[0].exitCode || 1);
	}

	console.log(`\nDone.`);
}

main().catch((err) => {
	console.error('Ticket runner failed:', err);
	process.exit(1);
});
