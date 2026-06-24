/**
 * Ticket discovery and parsing.
 *
 * Encapsulates the on-disk shape of a ticket: stage folder, optional sequence
 * prefix, slug, and the `prereq:` header field.  All filesystem-touching reads
 * for the snapshot live here.
 */

import { readdir, readFile, access } from 'node:fs/promises';
import { join, basename } from 'node:path';
import { constants } from 'node:fs';

/** Default stages from which to pull tickets (backlog excluded — parked by design). */
export const PENDING_STAGES = ['review', 'implement', 'fix', 'plan'];

/** All valid stage names (for --stages validation). */
export const KNOWN_STAGES = ['backlog', 'fix', 'plan', 'implement', 'review', 'complete', 'blocked'];

/** Map from stage → next stage in the pipeline (for prompt context). */
export const NEXT_STAGE = {
	backlog: 'plan',
	fix: 'implement',
	plan: 'implement',
	implement: 'review',
	review: 'complete',
};

/**
 * Pipeline rank for cross-stage prereq satisfaction.  `fix` and `plan` share
 * rank 1 because they're peer feeders into `implement` — neither is "ahead"
 * of the other.  `blocked` is intentionally absent: a prereq parked in
 * `blocked/` is treated as unsatisfied regardless of the dependent's stage.
 */
export const STAGE_RANK = {
	backlog: 0,
	fix: 1,
	plan: 1,
	implement: 2,
	review: 3,
	complete: 4,
};

/**
 * Cross-stage prereq satisfaction.
 *
 * A prereq P satisfies a dependent T (cross-stage) when P sits in a strictly
 * later pipeline rank than T.  Same-stage edges return `true` here because
 * in-stage ordering is enforced separately by the topo sort.  Anything in
 * `blocked/`, in `backlog/` (rank 0 < anything), or in a peer-but-different
 * stage (e.g. T in plan, P in fix — both rank 1) returns `false`.
 *
 * Stage names not in `STAGE_RANK` (notably `blocked`) are treated as
 * unsatisfied.
 */
export function isPrereqSatisfied(prereqStage, ticketStage) {
	if (prereqStage === ticketStage) return true;  // in-stage handled by topo sort
	const pr = STAGE_RANK[prereqStage];
	const tr = STAGE_RANK[ticketStage];
	if (pr == null || tr == null) return false;
	return pr > tr;
}

/**
 * One-shot scan of every known stage folder, returning `slug → { stage, file }`.
 *
 * When the same slug appears in multiple stages (e.g. an agent split or a
 * stale duplicate), the most-advanced copy wins — iteration runs in reverse
 * pipeline order so a slug found in `complete/` masks one still sitting in
 * `plan/`.  Used to resolve cross-stage prereq edges that aren't in the
 * snapshot's own stage bucket.
 *
 * Pass `{ withPrereqs: true }` to also read each ticket's `prereq:` header,
 * which lets callers walk the prereq DAG across stages (e.g. transitive
 * blocked-detection).  This costs one read per ticket and is opt-in.
 */
const STAGE_INDEX_ORDER = ['complete', 'review', 'implement', 'fix', 'plan', 'backlog', 'blocked'];
export async function indexAllTickets(ticketsDir, { withPrereqs = false } = {}) {
	const index = new Map();
	for (const stage of STAGE_INDEX_ORDER) {
		const stageDir = join(ticketsDir, stage);
		let entries;
		try {
			entries = await readdir(stageDir);
		} catch {
			continue;
		}
		for (const entry of entries) {
			if (!entry.endsWith('.md')) continue;
			const slug = parseSlug(entry);
			if (index.has(slug)) continue;
			const record = { stage, file: entry };
			if (withPrereqs) {
				try {
					const content = await readFile(join(stageDir, entry), 'utf-8');
					record.prereqs = parsePrereqs(content);
				} catch (err) {
					if (err.code === 'ENOENT') continue;  // raced with a remove/move
					throw err;
				}
			}
			index.set(slug, record);
		}
	}
	return index;
}

/**
 * Walk a ticket's prereq chain across the cross-stage index and return the
 * first slug that's parked in `blocked/`, or `null` if no path leads there.
 *
 * The index must be built with `{ withPrereqs: true }` so we have each
 * ticket's outgoing edges.  Slugs absent from the index terminate that
 * branch (assumed already complete or a stale reference).  Cycles are
 * tolerated via the visited set; the topo sort is responsible for rejecting
 * cycles among in-snapshot tickets.
 */
export function findTransitiveBlocker(ticket, index) {
	const visited = new Set();
	const stack = [...ticket.prereqs];
	while (stack.length > 0) {
		const slug = stack.pop();
		if (visited.has(slug)) continue;
		visited.add(slug);
		const found = index.get(slug);
		if (!found) continue;
		if (found.stage === 'blocked') return { slug };
		if (Array.isArray(found.prereqs)) {
			for (const p of found.prereqs) stack.push(p);
		}
	}
	return null;
}

/**
 * Resolve a ticket's prereqs against the cross-stage index and return the
 * first one that's *behind* (lower rank, peer-but-different stage, or
 * parked in `blocked/`).  Returns `null` when every prereq is either
 * satisfied (same stage or strictly later) or absent from the index
 * (assumed already complete or a stale reference).
 *
 * Pass a prebuilt index to avoid re-scanning when checking many tickets;
 * omit it for one-shot checks at the moment of processing.
 */
export async function findUnsatisfiedPrereq(ticket, ticketsDir, index) {
	const idx = index ?? await indexAllTickets(ticketsDir);
	for (const slug of ticket.prereqs) {
		const found = idx.get(slug);
		if (!found) continue;
		if (!isPrereqSatisfied(found.stage, ticket.stage)) {
			return { slug, stage: found.stage };
		}
	}
	return null;
}

const SEQUENCE_PREFIX = /^(\d+(?:\.\d+)?)-(.+)\.md$/;

/** Parse sequence number from filename. Returns null when no numeric prefix is present. */
export function parseSequence(filename) {
	const match = basename(filename).match(SEQUENCE_PREFIX);
	return match ? parseFloat(match[1]) : null;
}

/** Extract the canonical slug (filename without any numeric prefix or .md extension). */
export function parseSlug(filename) {
	const base = basename(filename, '.md');
	const match = base.match(/^\d+(?:\.\d+)?-(.+)$/);
	return match ? match[1] : base;
}

/** Parse the `prereq:` header field into an array of slug strings.  Tolerates legacy `dependencies:`. */
export function parsePrereqs(content) {
	// Header sits above the first `----` divider; parse only that region.
	const divIdx = content.indexOf('\n----');
	const header = divIdx === -1 ? content : content.slice(0, divIdx);
	const match = header.match(/^(?:prereq|dependencies):\s*(.*)$/mi);
	if (!match) return [];
	return match[1]
		.split(',')
		.map(s => s.trim())
		.filter(Boolean)
		// Defensive: strip any lingering `N-` or `N.N-` prefix and `.md` suffix.
		.map(ref => ref.replace(/^\d+(?:\.\d+)?-/, '').replace(/\.md$/, ''));
}

/**
 * Parse the optional `difficulty:` header field.  This is the portable,
 * agent-agnostic knob (`easy` | `medium` | `hard`); the runner maps it — in
 * combination with the pipeline stage and per-agent config — to a concrete
 * model and reasoning-effort (see lib/model-selection.mjs).  Returns the
 * trimmed lowercase value or `null` when absent; normalization to a known
 * token (and the `medium` default) happens at resolution time.
 */
export function parseDifficulty(content) {
	const divIdx = content.indexOf('\n----');
	const header = divIdx === -1 ? content : content.slice(0, divIdx);
	const match = header.match(/^difficulty:\s*(.*)$/mi);
	if (!match) return null;
	const value = match[1].trim().toLowerCase();
	return value || null;
}

/**
 * Look for a ticket with the given slug across the named stage folders.
 * Returns the first match (in the order `stages` was passed) as a fully-
 * populated ticket object, or null if no match exists.
 *
 * Used by the chase strategy after each stage transition to locate the
 * agent's same-slug successor — by name rather than by filesystem diff,
 * since other agents may be modifying tickets/ in parallel.
 */
export async function findTicketBySlug(ticketsDir, slug, stages) {
	for (const stage of stages) {
		const stageDir = join(ticketsDir, stage);
		let entries;
		try {
			entries = await readdir(stageDir);
		} catch {
			continue;  // stage dir doesn't exist
		}
		for (const entry of entries) {
			if (!entry.endsWith('.md')) continue;
			if (parseSlug(entry) !== slug) continue;
			const path = join(stageDir, entry);
			let content;
			try {
				content = await readFile(path, 'utf-8');
			} catch (err) {
				if (err.code === 'ENOENT') continue;  // raced with a remove/move
				throw err;
			}
			return {
				file: entry,
				path,
				stage,
				sequence: parseSequence(entry),
				slug,
				prereqs: parsePrereqs(content),
				difficulty: parseDifficulty(content),
			};
		}
	}
	return null;
}

/** Discover all .md ticket files in a stage folder, filtered by max sequence. */
export async function discoverTickets(ticketsDir, stage, maxSequence) {
	const stageDir = join(ticketsDir, stage);
	try {
		await access(stageDir, constants.R_OK);
	} catch {
		return [];
	}

	const entries = await readdir(stageDir);
	const tickets = [];

	for (const entry of entries) {
		if (!entry.endsWith('.md')) continue;

		const sequence = parseSequence(entry);
		// Unnumbered tickets are treated as sequence = +Infinity ("follows numbered").
		const effective = sequence ?? Infinity;
		if (effective > maxSequence) continue;

		const path = join(stageDir, entry);
		let content;
		try {
			content = await readFile(path, 'utf-8');
		} catch (err) {
			if (err.code === 'ENOENT') continue;  // raced with a remove/move during snapshotting
			throw err;
		}
		tickets.push({
			file: entry,
			path,
			stage,
			sequence,            // raw: number or null
			slug: parseSlug(entry),
			prereqs: parsePrereqs(content),
			difficulty: parseDifficulty(content),
		});
	}

	// Within a stage: ascending sequence (low first); unnumbered (null) sorts last.
	tickets.sort((a, b) => (a.sequence ?? Infinity) - (b.sequence ?? Infinity));
	return tickets;
}

export function formatSeq(seq) {
	return seq == null ? '--' : String(seq);
}
