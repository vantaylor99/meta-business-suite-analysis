/**
 * Command-line argument parsing and help output.
 */

import { KNOWN_STAGES, PENDING_STAGES } from './tickets.mjs';
import { KNOWN_STRATEGIES, DEFAULT_STRATEGY } from './strategies/index.mjs';
import { DEFAULT_PRUNE_AGE_DAYS } from './prune-completed.mjs';

export function printHelp() {
	const lines = [
		'Ticket Runner — process outstanding tickets via agentic CLI',
		'',
		'Default (`--strategy live`): the runner re-discovers and re-prioritizes the',
		'whole ticket board after every transition, so tickets created mid-run (a review',
		'that files a fix, a plan that splits) are picked up and re-ranked immediately.',
		'The `batch` and `chase` strategies instead snapshot the ticket list once at',
		'startup — tickets created during the run roll into the next run.',
		'',
		'Numeric filename prefix encodes sequence (lower runs sooner); prefix is optional.',
		'Unnumbered tickets run after all numbered ones in a stage.  Tickets may declare',
		'`prereq: <slug>, <slug>` in the header — prereqs run before dependents, and a',
		'sequence number that conflicts with a prereq edge is a hard error.',
		'',
		'Usage: node tess/scripts/run.mjs [options]',
		'',
		'Options:',
		'  --max-sequence <n>   Default max sequence for all stages  (default: unlimited)',
		'                       Tickets with sequence > n are skipped; unnumbered tickets',
		'                       are skipped whenever n is finite.',
		'  --stages <list>      Comma-separated stages, optionally with per-stage max sequence',
		'                       as  stage:n  (default: fix,review,implement,plan).  The order',
		'                       is the cross-stage priority — earlier stages run first.',
		'                       e.g.  --stages review:5,implement:3,fix',
		'                             --stages backlog:2  (backlog is not in the default set)',
		'  --agent <name>       claude | auggie | cursor | codex      (default: claude)',
		'  --strategy <name>    live | batch | chase                   (default: live)',
		'                       live:  re-discover and re-prioritize the whole board after',
		'                              every transition; mid-run tickets are picked up and',
		'                              re-ranked. Not snapshot-bound.',
		'                       batch: snapshot at startup; drain each stage before moving',
		'                              to the next (one transition per ticket per run).',
		'                       chase: snapshot at startup; take one root ticket and follow',
		'                              it through every stage to complete/ before the next.',
		'                              A ticket landing in blocked/ or backlog/ is deferred',
		'                              and any queued ticket listing it as `prereq:` is',
		'                              skipped for the rest of the run.',
		'  --max <n>            Stop after at most n tickets          (default: unlimited)',
		'                       (live: caps stage transitions, not snapshot size.)',
		'  --token-budget <n>   Soft per-ticket context budget (claude only).  When the',
		'                       running context size crosses n tokens, a one-shot',
		'                       BUDGET_WARNING is injected via a PreToolUse hook so the',
		'                       agent splits residual work into continuation tickets.',
		'                       In `chase`, splits land in the same stage and are picked',
		'                       up next within the chain.  In `batch`, splits roll into',
		'                       the next run.                          (default: unset)',
		'  --no-commit          Skip automatic git commit after each ticket',
		'  --skip-blocked       Pre-filter the snapshot: drop any ticket whose prereq',
		'                       chain reaches a slug parked in blocked/.  The runtime',
		'                       cross-stage prereq gate still applies to other misses',
		'                       (e.g. prereq still in plan/ when ticket is in implement/).',
		'  --refresh-index      Run the local code indexer incrementally before each',
		'                       ticket (no-op if tickets/.index/ does not exist).',
		`  --prune-completed-days <n>  Remove completed tickets whose landing commit is`,
		`                       older than n days, once per run  (default: ${DEFAULT_PRUNE_AGE_DAYS}).`,
		'  --no-prune-completed Skip the stale-completed-ticket sweep entirely.',
		'  --dry-run            List tickets without invoking agent',
		'  --help               Show this help',
	];
	console.log(lines.join('\n'));
}

/**
 * Parse --stages value into an ordered array of { stage, maxSequence } entries.
 * Bare stage names use the global defaultMax.
 */
export function parseStages(raw, defaultMax) {
	return raw.split(',').map(token => {
		const [stage, pStr] = token.trim().split(':');
		const maxSequence = pStr !== undefined ? parseFloat(pStr) : defaultMax;
		return { stage, maxSequence };
	});
}

export function parseArgs(argv) {
	const opts = {
		maxSequence: Infinity,
		agent: 'claude',
		strategy: DEFAULT_STRATEGY,
		dryRun: false,
		noCommit: false,
		skipBlocked: false,
		refreshIndex: false,
		maxTickets: Infinity,
		tokenBudget: Infinity,
		stagesRaw: null,
		pruneCompleted: true,
		pruneCompletedDays: DEFAULT_PRUNE_AGE_DAYS,
	};

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		switch (arg) {
			case '--max-sequence':
				opts.maxSequence = parseFloat(argv[++i]);
				break;
			case '--agent':
				opts.agent = argv[++i];
				break;
			case '--strategy':
				opts.strategy = argv[++i];
				break;
			case '--dry-run':
				opts.dryRun = true;
				break;
			case '--no-commit':
				opts.noCommit = true;
				break;
			case '--skip-blocked':
				opts.skipBlocked = true;
				break;
			case '--refresh-index':
				opts.refreshIndex = true;
				break;
			case '--prune-completed-days':
				opts.pruneCompletedDays = parseFloat(argv[++i]);
				break;
			case '--no-prune-completed':
				opts.pruneCompleted = false;
				break;
			case '--max':
				opts.maxTickets = parseInt(argv[++i], 10);
				break;
			case '--token-budget':
				opts.tokenBudget = parseInt(argv[++i], 10);
				break;
			case '--stages':
				opts.stagesRaw = argv[++i];
				break;
			case '--help':
				printHelp();
				process.exit(0);
		}
	}

	const stagesRaw = opts.stagesRaw ?? PENDING_STAGES.join(',');
	const stages = parseStages(stagesRaw, opts.maxSequence);

	for (const { stage } of stages) {
		if (!KNOWN_STAGES.includes(stage)) {
			console.error(`Unknown stage: "${stage}". Valid stages: ${KNOWN_STAGES.join(', ')}`);
			process.exit(1);
		}
	}

	if (!KNOWN_STRATEGIES.includes(opts.strategy)) {
		console.error(`Unknown strategy: "${opts.strategy}". Valid strategies: ${KNOWN_STRATEGIES.join(', ')}`);
		process.exit(1);
	}

	if (Number.isFinite(opts.tokenBudget) && opts.tokenBudget <= 0) {
		console.error(`--token-budget must be a positive integer.`);
		process.exit(1);
	}

	if (!Number.isFinite(opts.pruneCompletedDays) || opts.pruneCompletedDays < 0) {
		console.error(`--prune-completed-days must be a non-negative number.`);
		process.exit(1);
	}

	return { ...opts, stages };
}

export function formatStageSummary(stages) {
	return stages.map(({ stage, maxSequence }) =>
		Number.isFinite(maxSequence) ? `${stage}(<=${maxSequence})` : stage
	).join(', ');
}
