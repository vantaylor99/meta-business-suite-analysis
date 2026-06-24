/**
 * Git operations: tess version stamp, per-ticket commit, migration commit.
 */

import { execSync } from 'node:child_process';
import { migrate, needsMigration, FORMAT_VERSION } from '../migrate.mjs';

/** Short sha of the tess submodule's HEAD, for the run banner. */
export function getTessVersion(tessRoot) {
	try {
		const hash = execSync('git log -1 --format=%h', { cwd: tessRoot, encoding: 'utf-8' }).trim();
		return hash;
	} catch {
		return 'unknown';
	}
}

/** Default ceiling on file deletions a single ticket commit may capture.  A transient or
 *  partial working tree (the cause of the engine-wipe incident) surfaces as a mass deletion
 *  far above any legitimate single-ticket change.  Override with TESS_MAX_DELETIONS. */
const DEFAULT_MAX_DELETIONS = 100;

/** Count files marked for deletion (staged or worktree) in `git status --porcelain` output. */
function countDeletions(status) {
	return status.split('\n').filter(line => line[0] === 'D' || line[1] === 'D').length;
}

/** Stage and commit all changes for a completed ticket.  Returns true if a commit was created. */
export function commitTicket(ticket, cwd) {
	try {
		// Check if there are any changes to commit
		const status = execSync('git status --porcelain', { cwd, encoding: 'utf-8' }).trim();
		if (!status) return false;

		// Safety guard: refuse to capture a spurious mass deletion (e.g. a transient/partial
		// working tree that drops a whole package).  Checked before `git add -A`, so on abort
		// nothing is staged and the working tree is left untouched for inspection.
		const deletions = countDeletions(status);
		const maxDeletions = Number(process.env.TESS_MAX_DELETIONS ?? DEFAULT_MAX_DELETIONS);
		if (deletions > maxDeletions) {
			console.error(`[runner] ABORTING commit for ${ticket.slug}: ${deletions} deletions exceed the safety threshold (${maxDeletions}).`);
			console.error('[runner] This looks like a spurious mass-deletion (transient/partial working tree), not a ticket change.');
			console.error('[runner] Nothing was staged or committed; inspect with `git status` and re-run once the tree is intact.');
			console.error('[runner] If the deletion is genuinely intended, raise TESS_MAX_DELETIONS and re-run.');
			return false;
		}

		execSync('git add -A', { cwd, encoding: 'utf-8' });
		const msg = `ticket(${ticket.stage}): ${ticket.slug}`;
		execSync(`git commit -m "${msg}"`, { cwd, encoding: 'utf-8' });
		return true;
	} catch (err) {
		console.error(`[runner] Git commit failed: ${err.message}`);
		return false;
	}
}

/** Run migration if needed and commit the result.  Returns whether a commit was made. */
export async function runMigrationIfNeeded(ticketsDir, repoRoot, { noCommit, dryRun }) {
	if (!await needsMigration(ticketsDir)) return false;
	console.log('\n  Legacy ticket format detected — running migration to v' + FORMAT_VERSION + '...');
	const result = await migrate(ticketsDir, { dryRun });
	if (dryRun) {
		console.log(`    [dry-run] Would migrate ${result.migrated} ticket(s), rewrite ${result.rewrites} body/bodies.`);
		console.log('    Note: schedule below uses current (pre-migration) filenames and new ascending-seq');
		console.log('          ordering — it is REVERSED from what a real run will actually execute. To');
		console.log('          preview accurately: run `node tess/scripts/migrate.mjs`, commit, then re-dry-run.');
		return false;
	}
	console.log(`    Renamed ${result.renamed} ticket(s); rewrote ${result.rewrites} body/bodies; stamped .version=${FORMAT_VERSION}.`);
	if (noCommit) return false;
	try {
		const status = execSync('git status --porcelain', { cwd: repoRoot, encoding: 'utf-8' }).trim();
		if (!status) return false;
		execSync('git add -A', { cwd: repoRoot, encoding: 'utf-8' });
		execSync(`git commit -m "tess: migrate ticket format to v${FORMAT_VERSION}"`, { cwd: repoRoot, encoding: 'utf-8' });
		console.log('    Committed migration.');
		return true;
	} catch (err) {
		console.error(`    Migration commit failed: ${err.message}`);
		return false;
	}
}
