#!/usr/bin/env node
/**
 * Tess local code indexer.
 *
 * Walks tracked files (git ls-files), chunks them, embeds with a local
 * sentence-transformers model, and stores vectors in
 * tickets/.index/index.db (sqlite + sqlite-vec).
 *
 * Incremental by default: files whose content hash matches the stored row
 * are skipped.  --rebuild drops and re-creates the DB.
 *
 * Usage:
 *   node tess/scripts/index.mjs                # incremental refresh
 *   node tess/scripts/index.mjs --rebuild      # drop and rebuild
 *   node tess/scripts/index.mjs --status       # show counts
 *   node tess/scripts/index.mjs --watch        # debounced fs watcher
 */

import { readFile, stat, rm, mkdir, watch } from 'node:fs/promises';
import { join, resolve, sep, posix } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';

import { IndexStore } from './lib/index-store.mjs';
import { chunkText } from './lib/chunker.mjs';
import { Embedder, DEFAULT_MODEL, DEFAULT_DIM } from './lib/embedder.mjs';

export const DEFAULT_EXTS = new Set([
	'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs',
	'.py', '.rs', '.go', '.java', '.kt', '.swift',
	'.c', '.h', '.cpp', '.hpp', '.cc',
	'.rb', '.php', '.cs', '.scala',
	'.md', '.mdx', '.txt', '.rst',
	'.sql', '.tla',
	'.toml', '.yaml', '.yml', '.json',
	'.svelte', '.vue',
	'.sh', '.bash', '.ps1',
]);

export const ALWAYS_EXCLUDE = [
	'node_modules/', 'dist/', 'build/', 'out/', 'target/',
	'.git/',
	'tickets/', // tess working state, not project source.
	'team/',    // teamos working state (chat/todos/events).  Same prose-dominates-
	            // -code-rankings problem as tickets/.
	'docs/',    // long-form prose dominates the embedding signal vs. actual
	            // source — same problem as tickets/.  Projects whose docs/
	            // contains material the agent should search can re-include
	            // it via tickets/index-config.json (see CONFIG_FILENAME).
	'.next/', '.svelte-kit/', '.cache/', 'coverage/',
];

const CONFIG_FILENAME = 'index-config.json';

/**
 * Per-project index config, loaded from tickets/index-config.json:
 *   {
 *     "exclude":    ["examples/", "vendor/"],     // additional dir prefixes to skip
 *     "include":    ["docs/architecture/"],       // re-include paths under an excluded prefix
 *     "extensions": [".graphql", ".proto"]        // additional file extensions beyond DEFAULT_EXTS
 *   }
 *
 * `exclude` and `include` use directory-prefix matching (trailing "/" added
 * if missing), same semantic as ALWAYS_EXCLUDE.  `include` is checked before
 * exclude — any matching include exempts the path from the exclude list.
 * Extensions are normalized to lowercase with a leading dot.
 */
export async function loadIndexConfig(repoRoot) {
	const path = join(repoRoot, 'tickets', CONFIG_FILENAME);
	let raw;
	try { raw = await readFile(path, 'utf-8'); }
	catch (err) {
		if (err.code === 'ENOENT') return { source: null, exclude: [], include: [], extensions: [] };
		throw new Error(`Failed to read ${path}: ${err.message}`);
	}
	let parsed;
	try { parsed = JSON.parse(raw); }
	catch (err) { throw new Error(`Invalid JSON in ${path}: ${err.message}`); }

	const norm = (s) => {
		const p = String(s).replace(/\\/g, '/');
		return p.endsWith('/') ? p : p + '/';
	};
	const ext = (s) => {
		const e = String(s).toLowerCase();
		return e.startsWith('.') ? e : '.' + e;
	};
	return {
		source: path,
		exclude:    Array.isArray(parsed.exclude)    ? parsed.exclude.map(norm) : [],
		include:    Array.isArray(parsed.include)    ? parsed.include.map(norm) : [],
		extensions: Array.isArray(parsed.extensions) ? parsed.extensions.map(ext) : [],
	};
}

/**
 * Returns a `shouldIndex(relPath)` predicate combining ALWAYS_EXCLUDE,
 * DEFAULT_EXTS, and project config.  Pure factory — separable from
 * filesystem so the same logic powers tests and the watcher.
 */
export function makeShouldIndex(config) {
	const allExcludes = [...ALWAYS_EXCLUDE, ...config.exclude];
	const allExts = new Set([...DEFAULT_EXTS, ...config.extensions]);
	return function shouldIndex(relPath) {
		const p = relPath.split(sep).join(posix.sep);
		const dot = p.lastIndexOf('.');
		if (dot < 0) return false;
		const ext = p.slice(dot).toLowerCase();
		if (!allExts.has(ext)) return false;

		// Re-includes override excludes.
		for (const inc of config.include) {
			if (p.startsWith(inc) || p.includes('/' + inc)) return true;
		}
		for (const ex of allExcludes) {
			if (p.startsWith(ex) || p.includes('/' + ex)) return false;
		}
		return true;
	};
}

const MAX_FILE_BYTES = 256 * 1024;
const WATCH_DEBOUNCE_MS = 1500;

// ─── Discovery ─────────────────────────────────────────────────────────────────

function gitListFiles(repoRoot) {
	const out = execFileSync('git', ['ls-files', '-z'], {
		cwd: repoRoot,
		encoding: 'buffer',
		maxBuffer: 256 * 1024 * 1024,
	});
	return out.toString('utf-8').split('\0').filter(Boolean);
}

function hashContent(buf) {
	return createHash('sha256').update(buf).digest('hex');
}

function looksBinary(buf) {
	const len = Math.min(buf.length, 8192);
	for (let i = 0; i < len; i++) {
		if (buf[i] === 0) return true;
	}
	return false;
}

// ─── Indexer core ──────────────────────────────────────────────────────────────

/**
 * Run one pass of the indexer.  Returns { scanned, reindexed, pruned, chunks }.
 *
 * @param {object} opts
 * @param {string} opts.repoRoot
 * @param {string} opts.dbPath
 * @param {string} opts.modelCacheDir
 * @param {boolean} [opts.rebuild]
 * @param {(msg: string) => void} [opts.log]
 */
export async function runIndexer(opts) {
	const log = opts.log ?? (() => {});
	const repoRoot = resolve(opts.repoRoot);
	const dbPath = opts.dbPath;
	const modelCacheDir = opts.modelCacheDir;

	if (opts.rebuild) {
		await rm(dbPath, { force: true });
		log(`Removed existing index at ${dbPath}`);
	}

	const config = opts.config ?? await loadIndexConfig(repoRoot);
	if (config.source) {
		log(`Loaded index config: ${config.source}`);
		if (config.exclude.length)    log(`  +exclude:    ${config.exclude.join(', ')}`);
		if (config.include.length)    log(`  +include:    ${config.include.join(', ')}`);
		if (config.extensions.length) log(`  +extensions: ${config.extensions.join(', ')}`);
	}
	const shouldIndex = makeShouldIndex(config);

	const tracked = gitListFiles(repoRoot).filter(shouldIndex);
	log(`Found ${tracked.length} tracked file(s) eligible for indexing`);

	const store = await IndexStore.open(dbPath, {
		dim: DEFAULT_DIM,
		modelId: DEFAULT_MODEL,
	});

	let embedder = null;
	let scanned = 0, reindexed = 0, pruned = 0, totalChunks = 0;

	try {
		const trackedSet = new Set(tracked);
		const stored = store.listFilePaths();
		for (const path of stored) {
			if (!trackedSet.has(path)) {
				store.deleteFile(path);
				pruned++;
			}
		}
		if (pruned > 0) log(`Pruned ${pruned} file(s) no longer tracked`);

		for (const relPath of tracked) {
			scanned++;
			const abs = join(repoRoot, relPath);
			let st;
			try { st = await stat(abs); } catch { continue; }
			if (!st.isFile() || st.size === 0) continue;
			if (st.size > MAX_FILE_BYTES) continue;

			const buf = await readFile(abs);
			if (looksBinary(buf)) continue;

			const hash = hashContent(buf);
			const existing = store.getFile(relPath);
			if (existing && existing.content_hash === hash) continue;

			const text = buf.toString('utf-8');
			const chunks = chunkText(text, relPath);
			if (chunks.length === 0) {
				store.deleteFile(relPath);
				continue;
			}

			if (!embedder) {
				log(`Loading embedding model (${DEFAULT_MODEL})…`);
				embedder = await Embedder.load(modelCacheDir);
			}

			const embeddings = await embedder.embed(chunks.map(c => c.text), DEFAULT_DIM);
			const rows = chunks.map((c, i) => ({ ...c, embedding: embeddings[i] }));
			store.replaceFile(relPath, hash, st.mtimeMs, rows);
			reindexed++;
			totalChunks += chunks.length;
			if (reindexed % 25 === 0) log(`  ${reindexed} file(s) re-embedded so far…`);
		}

		store.setMeta('last_refresh_iso', new Date().toISOString());
	} finally {
		store.close();
	}

	return { scanned, reindexed, pruned, chunks: totalChunks };
}

// ─── CLI ───────────────────────────────────────────────────────────────────────

function parseArgs(argv) {
	const opts = { mode: 'refresh', repoRoot: process.cwd() };
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--rebuild') opts.mode = 'rebuild';
		else if (a === '--status') opts.mode = 'status';
		else if (a === '--config') opts.mode = 'config';
		else if (a === '--watch') opts.mode = 'watch';
		else if (a === '--project' && argv[i + 1]) opts.repoRoot = resolve(argv[++i]);
		else if (a === '--help' || a === '-h') {
			console.log([
				'Tess local code indexer',
				'',
				'Usage:',
				'  node tess/scripts/index.mjs              # incremental refresh',
				'  node tess/scripts/index.mjs --rebuild    # drop and rebuild',
				'  node tess/scripts/index.mjs --status     # show counts',
				'  node tess/scripts/index.mjs --config     # show effective filter config',
				'  node tess/scripts/index.mjs --watch      # rebuild on change',
				'',
				'Options:',
				'  --project <dir>   Project root (default: cwd)',
				'',
				'Config: tickets/index-config.json (optional). Format:',
				'  { "exclude": [...], "include": [...], "extensions": [...] }',
			].join('\n'));
			process.exit(0);
		}
	}
	return opts;
}

function paths(repoRoot) {
	const indexDir = join(repoRoot, 'tickets', '.index');
	return {
		indexDir,
		dbPath: join(indexDir, 'index.db'),
		modelCacheDir: join(indexDir, 'models'),
	};
}

async function cmdStatus(repoRoot) {
	const { dbPath } = paths(repoRoot);
	try {
		const store = await IndexStore.open(dbPath, { dim: DEFAULT_DIM, modelId: DEFAULT_MODEL, readonly: true });
		const { files, chunks } = store.stats();
		const last = store.getMeta('last_refresh_iso') ?? '(never)';
		const model = store.getMeta('model_id') ?? '(unset)';
		store.close();
		console.log(`Index:  ${dbPath}`);
		console.log(`Model:  ${model}`);
		console.log(`Files:  ${files}`);
		console.log(`Chunks: ${chunks}`);
		console.log(`Last:   ${last}`);
	} catch (err) {
		console.error(`No index found at ${dbPath}`);
		console.error(`(${err.message})`);
		process.exit(1);
	}
}

async function cmdConfig(repoRoot) {
	const config = await loadIndexConfig(repoRoot);
	const allExcludes = [...ALWAYS_EXCLUDE, ...config.exclude];
	const allExts = [...DEFAULT_EXTS, ...config.extensions].sort();
	console.log(`Config:        ${config.source ?? '(none — using defaults)'}`);
	console.log(`Exclude (all): ${allExcludes.join(', ')}`);
	console.log(`  defaults:    ${ALWAYS_EXCLUDE.join(', ')}`);
	console.log(`  + project:   ${config.exclude.length ? config.exclude.join(', ') : '(none)'}`);
	console.log(`Include:       ${config.include.length ? config.include.join(', ') : '(none)'}`);
	console.log(`Extensions:    ${allExts.join(' ')}`);
	if (config.extensions.length) {
		console.log(`  + project:   ${config.extensions.join(' ')}`);
	}
}

async function cmdRefresh(repoRoot, { rebuild = false } = {}) {
	const { indexDir, dbPath, modelCacheDir } = paths(repoRoot);
	await mkdir(indexDir, { recursive: true });
	const t0 = Date.now();
	const result = await runIndexer({
		repoRoot, dbPath, modelCacheDir, rebuild,
		log: msg => console.log(msg),
	});
	const ms = Date.now() - t0;
	console.log(
		`Indexed ${result.reindexed}/${result.scanned} file(s), ` +
		`${result.chunks} chunk(s), pruned ${result.pruned}, in ${(ms / 1000).toFixed(1)}s.`,
	);
}

async function cmdWatch(repoRoot) {
	console.log(`Watching ${repoRoot} for changes (debounce ${WATCH_DEBOUNCE_MS}ms). Ctrl-C to stop.`);
	await cmdRefresh(repoRoot);

	let timer = null;
	const trigger = () => {
		if (timer) clearTimeout(timer);
		timer = setTimeout(async () => {
			timer = null;
			try { await cmdRefresh(repoRoot); }
			catch (err) { console.error(`refresh failed: ${err.message}`); }
		}, WATCH_DEBOUNCE_MS);
	};

	const watcher = watch(repoRoot, { recursive: true });
	for await (const evt of watcher) {
		if (!evt.filename) continue;
		const f = evt.filename.split(sep).join(posix.sep);
		if (f.includes('node_modules/') || f.includes('.git/') || f.includes('tickets/.index/') || f.includes('tickets/.logs/')) continue;
		trigger();
	}
}

async function main() {
	const opts = parseArgs(process.argv.slice(2));
	if (opts.mode === 'status') return cmdStatus(opts.repoRoot);
	if (opts.mode === 'config') return cmdConfig(opts.repoRoot);
	if (opts.mode === 'rebuild') return cmdRefresh(opts.repoRoot, { rebuild: true });
	if (opts.mode === 'watch') return cmdWatch(opts.repoRoot);
	return cmdRefresh(opts.repoRoot);
}

const isMain = import.meta.url === `file://${process.argv[1]?.replace(/\\/g, '/')}` ||
	import.meta.url.endsWith(process.argv[1]?.replace(/\\/g, '/'));
if (isMain) {
	main().catch(err => { console.error(err); process.exit(1); });
}
