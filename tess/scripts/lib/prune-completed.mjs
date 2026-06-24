/**
 * Prune stale tickets from tickets/complete/.
 *
 * Completed tickets are an archive of finished work; left unbounded the folder
 * grows forever.  This sweep removes any completed ticket whose landing commit
 * is older than a cutoff (default 30 days), keeping the archive recent.
 *
 * Age is measured by the file's most-recent git commit timestamp — not the
 * filesystem mtime, which a checkout resets — so it reflects when the ticket
 * actually landed in complete/.  Deletions are git-tracked, so anything pruned
 * stays recoverable from history.
 */

import { readdir, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { execSync } from 'node:child_process';

export const DEFAULT_PRUNE_AGE_DAYS = 30;

/** Last-commit unix timestamp (seconds) for a tracked path, or null if untracked/unknown. */
function lastCommitEpoch(path, cwd) {
	try {
		const out = execSync(`git log -1 --format=%ct -- "${path}"`, { cwd, encoding: 'utf-8' }).trim();
		if (!out) return null;
		const epoch = parseInt(out, 10);
		return Number.isFinite(epoch) ? epoch : null;
	} catch {
		return null;
	}
}

/** Stage just the complete/ deletions and commit them.  Returns true on commit. */
function commitPrune(count, maxAgeDays, repoRoot) {
	try {
		execSync('git add -A -- tickets/complete', { cwd: repoRoot, encoding: 'utf-8' });
		const status = execSync('git status --porcelain -- tickets/complete', { cwd: repoRoot, encoding: 'utf-8' }).trim();
		if (!status) return false;
		const msg = `tess: prune ${count} completed ticket(s) older than ${maxAgeDays} days`;
		execSync(`git commit -m "${msg}"`, { cwd: repoRoot, encoding: 'utf-8' });
		return true;
	} catch (err) {
		console.error(`[runner] Prune commit failed: ${err.message}`);
		return false;
	}
}

/**
 * Remove completed tickets older than `maxAgeDays` (by git landing date).
 *
 * In `dryRun`, reports what would be removed without touching the filesystem.
 * Untracked completed tickets (no commit history) are left alone — we can't
 * date them, and they were presumably just dropped in by hand.
 *
 * Returns `{ removed, files }` where `files` is the list of pruned filenames.
 */
export async function pruneCompletedTickets(ticketsDir, repoRoot, { maxAgeDays = DEFAULT_PRUNE_AGE_DAYS, dryRun = false, noCommit = false } = {}) {
	const completeDir = join(ticketsDir, 'complete');
	let entries;
	try {
		entries = await readdir(completeDir);
	} catch {
		return { removed: 0, files: [] };  // no complete/ folder yet
	}

	const cutoffEpoch = Math.floor(Date.now() / 1000) - maxAgeDays * 24 * 60 * 60;
	const stale = [];
	for (const entry of entries) {
		if (!entry.endsWith('.md')) continue;
		const path = join(completeDir, entry);
		const epoch = lastCommitEpoch(path, repoRoot);
		if (epoch == null) continue;          // untracked or unknown age — leave it
		if (epoch < cutoffEpoch) stale.push({ file: entry, path });
	}

	if (stale.length === 0) return { removed: 0, files: [] };

	const files = stale.map(s => s.file);
	if (dryRun) return { removed: stale.length, files, dryRun: true };

	for (const s of stale) {
		try {
			await unlink(s.path);
		} catch { /* race or permission; skip */ }
	}
	if (!noCommit) commitPrune(stale.length, maxAgeDays, repoRoot);

	return { removed: stale.length, files };
}
