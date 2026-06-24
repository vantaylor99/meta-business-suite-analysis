/**
 * Lazy wrapper that calls the indexer in incremental mode if (and only if)
 * a code-search index already exists for the project.  This is what
 * `--refresh-index` invokes between tickets — without an existing
 * `tickets/.index/index.db`, the call is a silent no-op so the flag is safe
 * to leave on for projects that have not opted into search.
 *
 * Errors from the indexer are caught and logged but never abort the run:
 * stale results are better than a crashed pipeline.
 */

import { join } from 'node:path';
import { access } from 'node:fs/promises';
import { constants } from 'node:fs';

export async function maybeRefreshIndex(repoRoot) {
	const dbPath = join(repoRoot, 'tickets', '.index', 'index.db');
	try { await access(dbPath, constants.R_OK); }
	catch { return; }

	let runIndexer;
	try {
		({ runIndexer } = await import('../index.mjs'));
	} catch (err) {
		console.warn(`  ⚠ refresh-index: indexer not available (${err.message})`);
		return;
	}

	const t0 = Date.now();
	try {
		const result = await runIndexer({
			repoRoot,
			dbPath,
			modelCacheDir: join(repoRoot, 'tickets', '.index', 'models'),
			log: () => {}, // silent in the runner
		});
		if (result.reindexed > 0 || result.pruned > 0) {
			const ms = Date.now() - t0;
			console.log(`  [index] refreshed ${result.reindexed} file(s), pruned ${result.pruned} (${(ms / 1000).toFixed(1)}s)`);
		}
	} catch (err) {
		console.warn(`  ⚠ refresh-index failed: ${err.message}`);
	}
}
