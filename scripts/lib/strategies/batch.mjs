/**
 * Batch strategy — drains the snapshot in topo/sequence order, advancing
 * each ticket exactly one stage.  This is the original tess behavior.
 *
 * Strategy contract:
 *   await run({ snapshot, ticketsDir, repoRoot, tessRoot, tessVersion,
 *               logsDir, opts })
 *   → returns { errors: [{ slug, exitCode }, ...] }
 *
 * On a hard agent failure the strategy adds the slug to the deferred set
 * and continues with the rest of the queue — independent tickets still get
 * a chance to run.  Dependents cascade out via the deferred set.  The
 * orchestrator surfaces any collected errors via a non-zero exit code.
 *
 * Deferred set: a slug enters `deferred` when its cross-stage prereq is
 * still behind, its dependent was deferred earlier in this run, or the
 * agent errored on it.  Subsequent tickets that list a deferred slug as
 * `prereq:` are skipped and themselves added to the set, so the gap
 * cascades through the queue just like the chase strategy's block/backlog
 * cascade.
 */

import { runOneStage } from '../run-ticket.mjs';

export async function run(ctx) {
	const { snapshot } = ctx;
	const deferred = new Set();
	const errors = [];

	for (let i = 0; i < snapshot.length; i++) {
		const ticket = snapshot[i];
		const label = `[${i + 1}/${snapshot.length}]`;

		const cascade = ticket.prereqs.find(p => deferred.has(p));
		if (cascade) {
			console.log(`\n  ${label} Deferred ${ticket.file}: prereq "${cascade}" was deferred earlier this run.\n`);
			deferred.add(ticket.slug);
			continue;
		}

		const outcome = await runOneStage(ticket, ctx, { label });

		if (outcome.kind === 'stopped') break;
		if (outcome.kind === 'agent-error') {
			console.error(`  ${label} Agent error on "${ticket.slug}" — deferring slug and continuing with independent tickets.`);
			deferred.add(ticket.slug);
			errors.push({ slug: ticket.slug, exitCode: outcome.exitCode });
		}
		if (outcome.kind === 'deferred') {
			deferred.add(ticket.slug);
		}

		if (i < snapshot.length - 1) {
			await new Promise(r => setTimeout(r, 500));
		}
	}

	return { errors };
}
