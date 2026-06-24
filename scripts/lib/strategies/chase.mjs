/**
 * Chase strategy — picks one ticket and follows it through every pipeline
 * stage in a single run, then moves to the next root ticket.  Where batch
 * is stage-major (drain plan/, then implement/, then review/), chase is
 * ticket-major (take a plan ticket all the way to complete/).
 *
 * Successor lookup is by slug, not by filesystem diff.  After each stage
 * transition, we look for the same slug in any forward-ranked stage (an
 * agent is free to jump fix/ → review/ in one shot), then in blocked/ and
 * backlog/.  This is robust against other agents touching tickets/ in
 * parallel — we don't try to attribute every new file to the agent we
 * just ran.
 *
 * Deferral cascade: a slug enters `deferred` when (a) the agent moved it
 * to blocked/ or backlog/ during the chain, (b) the cross-stage prereq
 * gate in `runOneStage` rejected it because a prereq is still behind, or
 * (c) the agent errored on it.  In all three cases the chain ends but the
 * run continues with the next root.  Subsequent root tickets that list a
 * deferred slug as a prereq are skipped — and they themselves are added
 * to `deferred`, so the skip cascades transitively through the queue.
 * Agent errors are also collected and surfaced as a non-zero exit code at
 * the end of the run via the strategy's returned `{ errors }`.
 *
 * A safety cap (MAX_CHAIN_STEPS) bounds how many stage transitions a
 * single chase can perform, in case an agent regresses a ticket
 * (e.g. implement → plan) and creates a loop.  The natural pipeline
 * tops out at 4 steps (backlog → plan → implement → review → complete).
 */

import { runOneStage } from '../run-ticket.mjs';
import { NEXT_STAGE, STAGE_RANK, findTicketBySlug, discoverTickets } from '../tickets.mjs';
import { topoSortAndCheck } from '../topo.mjs';

// Bumped from 6 to give budget-triggered same-stage continuations room to
// run alongside the natural 4-stage pipeline.  Still finite so a misbehaving
// agent that regresses tickets cannot loop forever.
const MAX_CHAIN_STEPS = 12;

/**
 * Pipeline-ordered list of stages strictly later in rank than `stage`.
 * Used to chase a slug forward when an agent jumps multiple stages in one
 * transition (e.g. fix/ → review/, skipping implement/).
 */
function forwardStages(stage) {
	const r = STAGE_RANK[stage];
	if (r == null) return [];
	return Object.entries(STAGE_RANK)
		.filter(([, rank]) => rank > r)
		.sort(([, a], [, b]) => a - b)
		.map(([s]) => s);
}

/**
 * After a budget-triggered split, find continuation tickets the agent left in
 * the source stage.  We identify "new" purely by path — anything in the source
 * stage that wasn't seen before this transition is a continuation candidate.
 */
async function findBudgetContinuations(ticketsDir, sourceStage, knownPaths) {
	const all = await discoverTickets(ticketsDir, sourceStage, Infinity);
	const fresh = all.filter(t => !knownPaths.has(t.path));
	if (fresh.length === 0) return [];
	try {
		return topoSortAndCheck(fresh);
	} catch (err) {
		console.warn(`  Continuation topo-sort failed (${err.message}); using filename order.`);
		return fresh;
	}
}

export async function run(ctx) {
	const { snapshot, ticketsDir } = ctx;

	const processed = new Set();   // slugs we've already chased (or skipped) as a root
	const deferred = new Set();    // slugs that hit blocked/backlog this run
	const errors = [];             // agent-error outcomes; surfaced as non-zero exit at end of run
	// Every ticket path the chain has ever seen (snapshot + continuations + advances).
	// Anything that appears in a source stage outside this set is a budget-induced
	// continuation we should chase next.
	const knownPaths = new Set(snapshot.map(t => t.path));

	rootLoop: for (let i = 0; i < snapshot.length; i++) {
		const root = snapshot[i];
		const rootLabel = `[root ${i + 1}/${snapshot.length}]`;

		if (processed.has(root.slug)) continue;

		const blockingPrereq = root.prereqs.find(p => deferred.has(p));
		if (blockingPrereq) {
			console.log(`\n  ${rootLabel} Skipped ${root.file}: prereq "${blockingPrereq}" is deferred this run.\n`);
			processed.add(root.slug);
			deferred.add(root.slug);  // cascade: anything depending on root is also deferred
			continue;
		}

		processed.add(root.slug);

		// Chain-as-queue: handles natural advances + budget-induced continuations
		// uniformly.  We push continuations to the front so the chain stays
		// depth-first within a single root.
		const chain = [root];
		let step = 0;
		while (chain.length > 0) {
			step++;
			if (step > MAX_CHAIN_STEPS) {
				console.log(`  Chain exceeded ${MAX_CHAIN_STEPS} steps for "${root.slug}" — moving on.`);
				break;
			}

			const t = chain.shift();
			if (!NEXT_STAGE[t.stage]) continue;  // terminal stage (e.g., complete)

			const stepLabel = `[root ${i + 1}/${snapshot.length} · step ${step}]`;
			const outcome = await runOneStage(t, ctx, { label: stepLabel });

			if (outcome.kind === 'stopped') break rootLoop;
			if (outcome.kind === 'skipped') break;
			if (outcome.kind === 'timed-out') break;
			if (outcome.kind === 'deferred') {
				deferred.add(t.slug);
				break;
			}
			if (outcome.kind === 'agent-error') {
				console.error(`  ${stepLabel} Agent error on "${t.slug}" — deferring slug and continuing with independent roots.`);
				deferred.add(t.slug);
				errors.push({ slug: t.slug, exitCode: outcome.exitCode });
				break;
			}

			const followUps = [];

			// Budget split: agent left continuation tickets in the source stage.
			if (outcome.budgetTriggered) {
				const continuations = await findBudgetContinuations(ticketsDir, t.stage, knownPaths);
				for (const c of continuations) knownPaths.add(c.path);
				if (continuations.length > 0) {
					console.log(`  Budget split: chasing ${continuations.length} new ${t.stage}/ continuation(s) before advancing.`);
					followUps.push(...continuations);
				}
			}

			// Natural advance: same slug in any forward-ranked stage.  The
			// agent is allowed to skip ahead (e.g. fix/ → review/ when no
			// implementation work is needed), so we accept a hit anywhere
			// past the current rank, earliest stage first.
			const advanced = await findTicketBySlug(ticketsDir, t.slug, forwardStages(t.stage));
			if (advanced) {
				knownPaths.add(advanced.path);
				followUps.push(advanced);
				if (advanced.stage !== NEXT_STAGE[t.stage]) {
					console.log(`  Chase: "${t.slug}" advanced ${t.stage}/ → ${advanced.stage}/ (skipped intermediate stage).`);
				}
			} else if (!outcome.budgetTriggered) {
				// No advance and no budget split — check if the agent parked the slug.
				const parked = await findTicketBySlug(ticketsDir, t.slug, ['blocked', 'backlog']);
				if (parked) {
					deferred.add(t.slug);
					console.log(`  Chase ended: "${t.slug}" landed in ${parked.stage}/. Dependents will be skipped this run.\n`);
				} else {
					console.log(`  Chase ended: no successor for "${t.slug}" past ${t.stage}/ (agent may have split or renamed it).\n`);
				}
			}

			// Continuations come before the natural successor: drain same-stage
			// work first, then walk forward through the pipeline.
			if (followUps.length > 0) chain.unshift(...followUps);
		}

		// Pause briefly between roots to mirror batch's between-ticket delay.
		if (i < snapshot.length - 1) {
			await new Promise(r => setTimeout(r, 500));
		}
	}

	return { errors };
}
