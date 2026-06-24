#!/usr/bin/env node
/**
 * Tess ticket format migration.
 *
 * v1 (legacy) → v2 (current):
 *   - Numeric prefix semantics: priority (higher = sooner) → sequence (lower = sooner)
 *   - Header field: `dependencies:` → `prereq:`
 *   - Inter-ticket references drop the numeric prefix (sequence may change)
 *
 * Detection:
 *   - tickets/.version missing → check for prefix-numbered tickets; migrate if any
 *   - tickets/.version < current FORMAT_VERSION → migrate
 *
 * Usage:
 *   node tess/scripts/migrate.mjs              # migrate project at cwd
 *   node tess/scripts/migrate.mjs --dry-run    # preview without writing
 *   node tess/scripts/migrate.mjs --project /path/to/project
 *
 * Idempotent — no-op if already at current version.
 */

import { readFile, writeFile, readdir, rename, mkdir, access } from 'node:fs/promises';
import { join, basename, resolve } from 'node:path';
import { constants } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const FORMAT_VERSION = 2;
export const VERSION_FILE = '.version';
export const TICKET_STAGES = ['backlog', 'fix', 'plan', 'implement', 'review', 'complete', 'blocked'];

const PREFIX_RE = /^(\d+(?:\.\d+)?)-(.+)\.md$/;

function log(msg) { console.log(`  ${msg}`); }

// ─── Version file ──────────────────────────────────────────────────────────────

export async function readVersion(ticketsDir) {
	try {
		const raw = await readFile(join(ticketsDir, VERSION_FILE), 'utf-8');
		const v = parseInt(raw.trim(), 10);
		return Number.isFinite(v) ? v : null;
	} catch {
		return null;
	}
}

export async function writeVersion(ticketsDir) {
	await mkdir(ticketsDir, { recursive: true });
	await writeFile(join(ticketsDir, VERSION_FILE), `${FORMAT_VERSION}\n`, 'utf-8');
}

// ─── Ticket discovery ──────────────────────────────────────────────────────────

async function scanTickets(ticketsDir) {
	const tickets = [];
	for (const stage of TICKET_STAGES) {
		const stageDir = join(ticketsDir, stage);
		let entries;
		try { entries = await readdir(stageDir); } catch { continue; }
		for (const entry of entries) {
			if (!entry.endsWith('.md')) continue;
			const m = entry.match(PREFIX_RE);
			const path = join(stageDir, entry);
			if (m) {
				tickets.push({ stage, file: entry, path, number: parseFloat(m[1]), slug: m[2], hasPrefix: true });
			} else {
				tickets.push({ stage, file: entry, path, number: null, slug: basename(entry, '.md'), hasPrefix: false });
			}
		}
	}
	return tickets;
}

// ─── Migration predicate ───────────────────────────────────────────────────────

export async function needsMigration(ticketsDir) {
	const v = await readVersion(ticketsDir);
	if (v != null) return v < FORMAT_VERSION;
	// No version file — treat as v1 only if there's content to migrate.
	for (const stage of TICKET_STAGES) {
		const stageDir = join(ticketsDir, stage);
		try {
			const entries = await readdir(stageDir);
			if (entries.some(f => PREFIX_RE.test(f))) return true;
		} catch { /* stage missing, skip */ }
	}
	return false;
}

// ─── Migration logic ───────────────────────────────────────────────────────────

function escapeRegex(s) {
	return s.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&');
}

/** Rewrite ticket content: rename `dependencies:` header, strip sequence prefix from references. */
function rewriteContent(content, slugs) {
	let out = content.replace(/^(dependencies):/m, 'prereq:');
	// Try longest slugs first so "foo-bar" matches before "foo"
	const ordered = [...slugs].sort((a, b) => b.length - a.length);
	for (const slug of ordered) {
		const re = new RegExp(`(^|[^\\w.-])\\d+(?:\\.\\d+)?-${escapeRegex(slug)}(?![\\w-])`, 'g');
		out = out.replace(re, (_, lead) => `${lead}${slug}`);
	}
	return out;
}

/**
 * Perform migration on the tickets directory.
 * Returns { status, migrated, renamed, rewrites, touched: [paths] }.
 *   status: 'up-to-date' | 'stamped' | 'migrated'
 */
export async function migrate(ticketsDir, { dryRun = false } = {}) {
	const currentVersion = await readVersion(ticketsDir);
	if (currentVersion != null && currentVersion >= FORMAT_VERSION) {
		return { status: 'up-to-date', migrated: 0, renamed: 0, rewrites: 0, touched: [] };
	}

	const tickets = await scanTickets(ticketsDir);
	const numbered = tickets.filter(t => t.hasPrefix);
	const slugs = new Set(tickets.map(t => t.slug));

	if (numbered.length === 0) {
		// No old-format numbered files — just stamp the version.
		if (!dryRun) await writeVersion(ticketsDir);
		return { status: 'stamped', migrated: 0, renamed: 0, rewrites: 0, touched: [] };
	}

	const nums = numbered.map(t => t.number);
	const min = Math.min(...nums);
	const max = Math.max(...nums);
	// Figure out the max decimal precision in the input so we can round-trip
	// through floating point without artifacts like 10.899999999999999.
	const maxDecimals = Math.max(0, ...nums.map(n => {
		const s = String(n);
		const i = s.indexOf('.');
		return i === -1 ? 0 : s.length - i - 1;
	}));
	for (const t of numbered) {
		t.newNumber = Number((max + min - t.number).toFixed(maxDecimals));
		t.newFile = `${t.newNumber}-${t.slug}.md`;
		t.newPath = join(ticketsDir, t.stage, t.newFile);
	}

	// Rewrite content (dependencies → prereq, strip prefixes from known-slug references).
	const rewrites = [];
	for (const t of tickets) {
		const original = await readFile(t.path, 'utf-8');
		const updated = rewriteContent(original, slugs);
		if (updated !== original) rewrites.push({ path: t.path, content: updated });
	}

	if (dryRun) {
		return {
			status: 'migrated',
			migrated: numbered.length,
			renamed: numbered.length,
			rewrites: rewrites.length,
			touched: [...rewrites.map(r => r.path), ...numbered.map(t => t.newPath)],
		};
	}

	for (const { path, content } of rewrites) {
		await writeFile(path, content, 'utf-8');
	}

	// Two-phase rename to avoid collisions when swapping (e.g., 2 ↔ 8).
	for (const t of numbered) {
		t.tmpPath = t.path + '.migrating';
		await rename(t.path, t.tmpPath);
	}
	for (const t of numbered) {
		await rename(t.tmpPath, t.newPath);
	}

	await writeVersion(ticketsDir);

	return {
		status: 'migrated',
		migrated: numbered.length,
		renamed: numbered.length,
		rewrites: rewrites.length,
		touched: [...rewrites.map(r => r.path), ...numbered.map(t => t.newPath)],
	};
}

// ─── CLI ───────────────────────────────────────────────────────────────────────

function parseArgs(argv) {
	const opts = { projectRoot: process.cwd(), dryRun: false };
	for (let i = 0; i < argv.length; i++) {
		if (argv[i] === '--project' && argv[i + 1]) {
			opts.projectRoot = resolve(argv[++i]);
		} else if (argv[i] === '--dry-run') {
			opts.dryRun = true;
		} else if (argv[i] === '--help') {
			console.log([
				'Tess ticket migration (v1 → v2: sequence semantics, prereq header)',
				'',
				'Usage:',
				'  node tess/scripts/migrate.mjs',
				'  node tess/scripts/migrate.mjs --dry-run',
				'  node tess/scripts/migrate.mjs --project /path/to/project',
				'',
				'Idempotent — no-op if already at current version.',
			].join('\n'));
			process.exit(0);
		}
	}
	return opts;
}

async function main() {
	const { projectRoot, dryRun } = parseArgs(process.argv.slice(2));
	const ticketsDir = join(projectRoot, 'tickets');

	try { await access(ticketsDir, constants.F_OK); }
	catch {
		console.error(`No tickets/ directory at ${projectRoot}`);
		process.exit(1);
	}

	console.log(`\nTess migrate — project: ${projectRoot}${dryRun ? '  (dry-run)' : ''}\n`);

	const result = await migrate(ticketsDir, { dryRun });
	switch (result.status) {
		case 'up-to-date':
			log('Already at current format version — nothing to do.');
			break;
		case 'stamped':
			log(`Stamped tickets/.version = ${FORMAT_VERSION} (no legacy tickets found).`);
			break;
		case 'migrated':
			log(`Renamed ${result.renamed} ticket(s) with inverted sequence.`);
			log(`Rewrote ${result.rewrites} ticket body/bodies.`);
			if (!dryRun) log(`Wrote tickets/.version = ${FORMAT_VERSION}.`);
			break;
	}
	console.log('\nDone.\n');
}

// Run as script iff invoked directly (not imported).
const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
	main().catch((err) => { console.error('Migration failed:', err); process.exit(1); });
}
