import type { Plugin } from 'vite';
import { readdir, readFile, access, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { constants } from 'node:fs';
import { spawn, type ChildProcess } from 'node:child_process';
import type { ServerResponse } from 'node:http';

interface ApiOptions {
	projectRoot: string;
	ticketsDir: string;
	siblingDir?: string;
	siblingPort?: number;
}

const STAGES = ['backlog', 'fix', 'plan', 'implement', 'review', 'blocked', 'complete'] as const;

function json(res: ServerResponse, data: unknown, status = 200) {
	res.writeHead(status, { 'Content-Type': 'application/json' });
	res.end(JSON.stringify(data));
}

async function dirExists(path: string): Promise<boolean> {
	try { await access(path, constants.F_OK); return true; } catch { return false; }
}

async function fileExists(path: string): Promise<boolean> {
	try { await access(path, constants.R_OK); return true; } catch { return false; }
}

// Extract optional numeric sequence prefix and slug from a ticket filename.
function parseFilename(filename: string): { sequence: number | null; slug: string } {
	const stem = filename.replace(/\.md$/, '');
	const match = stem.match(/^(\d+(?:\.\d+)?)-(.+)$/);
	if (!match) return { sequence: null, slug: stem };
	return { sequence: parseFloat(match[1]), slug: match[2] };
}

function parseTicketMeta(content: string): { meta: Record<string, string | string[]>; body: string } {
	const sepIdx = content.indexOf('----');
	if (sepIdx === -1) return { meta: {}, body: content.trim() };

	const header = content.slice(0, sepIdx);
	const body = content.slice(sepIdx + 4).trim();
	const meta: Record<string, string | string[]> = {};

	let currentKey = '';
	let listValues: string[] = [];
	let inList = false;

	for (const line of header.split('\n')) {
		const trimmed = line.trim();
		if (!trimmed) continue;

		if (trimmed.startsWith('- ') && inList) {
			listValues.push(trimmed.slice(2).trim());
			continue;
		}

		if (inList && currentKey) {
			meta[currentKey] = listValues.length === 1 ? listValues[0] : listValues;
			inList = false;
			listValues = [];
		}

		const colonIdx = trimmed.indexOf(':');
		if (colonIdx === -1) continue;

		currentKey = trimmed.slice(0, colonIdx).trim();
		const val = trimmed.slice(colonIdx + 1).trim();

		if (val) {
			if (val.includes(',')) {
				meta[currentKey] = val.split(',').map(s => s.trim());
			} else {
				meta[currentKey] = val;
			}
		} else {
			inList = true;
			listValues = [];
		}
	}

	if (inList && currentKey) {
		meta[currentKey] = listValues.length === 1 ? listValues[0] : listValues;
	}

	return { meta, body };
}

// Normalize a meta value (string or string[]) into a comma-joined string, or undefined.
function metaToString(val: string | string[] | undefined): string | undefined {
	if (val === undefined) return undefined;
	return Array.isArray(val) ? val.join(', ') : val;
}

async function listMdFiles(dir: string): Promise<string[]> {
	try {
		const files = await readdir(dir);
		return files.filter(f => f.endsWith('.md') && f !== 'AGENTS.md' && f !== 'CLAUDE.md').sort();
	} catch { return []; }
}

// ─── Search wiring ───────────────────────────────────────────────────────────

// Lazy-loaded handle to the shared search-tools module + its open-index ctx.
// The embedder is heavy to load (~155MB ONNX model); we only spin it up the
// first time the user actually searches, and we keep one shared ctx for the
// life of the dev server.  The server is read-only against the index, so
// concurrent searches are safe.
type SearchTools = typeof import('../../../scripts/lib/search-tools.mjs');
type SearchCtx = Awaited<ReturnType<SearchTools['openSearchIndex']>>;

interface SearchHandle {
	tools: SearchTools;
	ctx: SearchCtx;
}

class SearchUnavailableError extends Error {
	code = 'INDEX_NOT_BUILT';
}

async function loadSearchTools(): Promise<SearchTools> {
	// Path is relative to this compiled file. Vite picks up .mjs through Node
	// resolution when called from the dev-server middleware (Node context).
	return await import('../../../scripts/lib/search-tools.mjs');
}

async function openSearchHandle(projectRoot: string): Promise<SearchHandle> {
	const tools = await loadSearchTools();
	try {
		const ctx = await tools.openSearchIndex({ repoRoot: projectRoot });
		return { tools, ctx };
	} catch (err: any) {
		if (err?.code === 'INDEX_NOT_BUILT') {
			throw new SearchUnavailableError(err.message);
		}
		throw err;
	}
}

// ─── Index maintenance jobs ──────────────────────────────────────────────────

// Single-slot job tracker.  The indexer is CPU/IO heavy; running two at once
// would corrupt the DB.  A new request while a job is running returns 409.
type JobKind = 'refresh' | 'rebuild';
type JobStatus = 'running' | 'success' | 'error';

interface Job {
	id: string;
	kind: JobKind;
	status: JobStatus;
	startedAt: number;
	endedAt?: number;
	exitCode?: number;
	logTail: string[];          // ring buffer, capped
	child: ChildProcess | null;
}

const JOB_LOG_TAIL = 200;
let currentJob: Job | null = null;

function pushJobLog(job: Job, chunk: Buffer | string) {
	const text = typeof chunk === 'string' ? chunk : chunk.toString('utf-8');
	for (const line of text.split(/\r?\n/)) {
		if (!line) continue;
		job.logTail.push(line);
		if (job.logTail.length > JOB_LOG_TAIL) job.logTail.shift();
	}
}

function publicJob(job: Job | null) {
	if (!job) return null;
	return {
		id: job.id,
		kind: job.kind,
		status: job.status,
		startedAt: job.startedAt,
		endedAt: job.endedAt,
		exitCode: job.exitCode,
		logTail: job.logTail,
	};
}

function startIndexerJob(kind: JobKind, projectRoot: string): Job {
	const id = `${kind}-${Date.now()}`;
	const args = [join(projectRoot, 'tess', 'scripts', 'index.mjs')];
	if (kind === 'rebuild') args.push('--rebuild');

	const job: Job = {
		id,
		kind,
		status: 'running',
		startedAt: Date.now(),
		logTail: [],
		child: null,
	};

	const child = spawn(process.execPath, args, {
		cwd: projectRoot,
		stdio: ['ignore', 'pipe', 'pipe'],
		env: process.env,
	});
	job.child = child;

	child.stdout?.on('data', (c) => pushJobLog(job, c));
	child.stderr?.on('data', (c) => pushJobLog(job, c));
	child.on('error', (err) => {
		pushJobLog(job, `[spawn-error] ${err.message}`);
		job.status = 'error';
		job.exitCode = -1;
		job.endedAt = Date.now();
	});
	child.on('close', (code) => {
		job.exitCode = code ?? 0;
		job.status = code === 0 ? 'success' : 'error';
		job.endedAt = Date.now();
		job.child = null;
	});

	return job;
}

// ─── Index status ────────────────────────────────────────────────────────────

interface IndexStatusPayload {
	exists: boolean;
	dbPath: string;
	files?: number;
	chunks?: number;
	dim?: number;
	modelId?: string;
	schemaVersion?: string;
	dbSizeBytes?: number;
	dbModifiedMs?: number;
}

async function getIndexStatus(projectRoot: string): Promise<IndexStatusPayload> {
	const dbPath = join(projectRoot, 'tickets', '.index', 'index.db');
	if (!await fileExists(dbPath)) return { exists: false, dbPath };

	const tools = await loadSearchTools();
	let ctx: SearchCtx | null = null;
	try {
		ctx = await tools.openSearchIndex({ repoRoot: projectRoot });
		const stats = ctx.store.stats();
		const st = await stat(dbPath);
		return {
			exists: true,
			dbPath,
			files: stats.files,
			chunks: stats.chunks,
			dim: ctx.store.dim,
			modelId: ctx.store.getMeta('model_id') ?? undefined,
			schemaVersion: ctx.store.getMeta('schema_version') ?? undefined,
			dbSizeBytes: st.size,
			dbModifiedMs: st.mtimeMs,
		};
	} finally {
		ctx?.store?.close?.();
	}
}

// ─── Index config (effective filter rules) ──────────────────────────────────

interface IndexConfigPayload {
	source: string | null;            // path to project config file, or null
	defaults: { exclude: string[]; extensions: string[] };
	project: { exclude: string[]; include: string[]; extensions: string[] };
	effective: { exclude: string[]; include: string[]; extensions: string[] };
}

async function getIndexConfig(projectRoot: string): Promise<IndexConfigPayload> {
	// index.mjs exports the loader and the default constants; reuse them so
	// the UI cannot drift from what the indexer actually applies.
	const indexer = await import('../../../scripts/index.mjs');
	const cfg = await indexer.loadIndexConfig(projectRoot);
	const defaults = {
		exclude: [...indexer.ALWAYS_EXCLUDE],
		extensions: [...indexer.DEFAULT_EXTS].sort(),
	};
	return {
		source: cfg.source,
		defaults,
		project: {
			exclude: cfg.exclude,
			include: cfg.include,
			extensions: cfg.extensions,
		},
		effective: {
			exclude: [...defaults.exclude, ...cfg.exclude],
			include: cfg.include,
			extensions: [...new Set([...defaults.extensions, ...cfg.extensions])].sort(),
		},
	};
}

// ─── Plugin ──────────────────────────────────────────────────────────────────

export function tessApi(opts: ApiOptions): Plugin {
	const { projectRoot, ticketsDir, siblingDir } = opts;
	const siblingPort = opts.siblingPort ?? 3003;

	// Cached search handle. Held for the life of the dev server.  Reset on
	// rebuild (next request opens a fresh handle).
	let searchHandle: SearchHandle | null = null;
	async function getSearch(): Promise<SearchHandle> {
		if (!searchHandle) searchHandle = await openSearchHandle(projectRoot);
		return searchHandle;
	}
	function closeSearch() {
		try { searchHandle?.ctx.store.close(); } catch { /* already closed */ }
		searchHandle = null;
	}

	async function getPipeline() {
		const counts: Record<string, number> = {};
		for (const stage of STAGES) {
			counts[stage] = (await listMdFiles(join(ticketsDir, stage))).length;
		}
		return counts;
	}

	async function getStage(stage: string) {
		const dir = join(ticketsDir, stage);
		const files = await listMdFiles(dir);
		return Promise.all(files.map(async filename => {
			const content = await readFile(join(dir, filename), 'utf-8');
			const { meta } = parseTicketMeta(content);
			const { sequence, slug } = parseFilename(filename);
			const files = meta.files
				? (Array.isArray(meta.files) ? meta.files : [meta.files])
				: undefined;
			const prereq = metaToString(meta.prereq) ?? metaToString(meta.dependencies);
			return {
				filename,
				stage,
				sequence,
				slug,
				description: (meta.description as string) ?? slug,
				prereq,
				files,
			};
		}));
	}

	async function getTicket(stage: string, filename: string) {
		const filepath = join(ticketsDir, stage, filename);
		const raw = await readFile(filepath, 'utf-8');
		const { meta, body } = parseTicketMeta(raw);
		const { sequence, slug } = parseFilename(filename);
		const files = meta.files
			? (Array.isArray(meta.files) ? meta.files : [meta.files])
			: undefined;
		const prereq = metaToString(meta.prereq) ?? metaToString(meta.dependencies);
		return {
			filename,
			stage,
			sequence,
			slug,
			description: (meta.description as string) ?? slug,
			prereq,
			files,
			body,
			raw,
		};
	}

	async function getSibling() {
		if (!siblingDir || !await dirExists(siblingDir)) return null;
		return { name: 'teamos', url: `http://localhost:${siblingPort}` };
	}

	return {
		name: 'tess-api',
		configureServer(server) {
			server.middlewares.use(async (req, res, next) => {
				if (!req.url?.startsWith('/api/')) return next();

				const url = new URL(req.url, `http://${req.headers.host}`);
				const path = url.pathname;
				const method = (req.method ?? 'GET').toUpperCase();

				try {
					if (path === '/api/pipeline') {
						return json(res, await getPipeline());
					}

					if (path === '/api/sibling') {
						return json(res, await getSibling());
					}

					// ── Search ────────────────────────────────────────
					if (path === '/api/search' && method === 'GET') {
						const q = (url.searchParams.get('q') ?? '').trim();
						if (!q) return json(res, { error: 'q is required' }, 400);
						const k = clampInt(url.searchParams.get('k'), 5, 1, 50);
						const pathFilter = url.searchParams.get('path') || null;
						try {
							const { tools, ctx } = await getSearch();
							const matches = await tools.searchCode({ query: q, k, pathFilter }, ctx);
							return json(res, { query: q, k, pathFilter, matches });
						} catch (err) {
							if (err instanceof SearchUnavailableError) {
								return json(res, { error: 'index-not-built', message: err.message }, 503);
							}
							throw err;
						}
					}

					if (path === '/api/refs' && method === 'GET') {
						const symbol = (url.searchParams.get('q') ?? '').trim();
						if (!symbol) return json(res, { error: 'q is required' }, 400);
						const max = clampInt(url.searchParams.get('max'), 50, 1, 500);
						const pathFilter = url.searchParams.get('path') || null;
						try {
							const { tools, ctx } = await getSearch();
							const rows = tools.findReferences({ symbol, max, pathFilter }, ctx);
							return json(res, { symbol, max, pathFilter, rows });
						} catch (err) {
							if (err instanceof SearchUnavailableError) {
								return json(res, { error: 'index-not-built', message: err.message }, 503);
							}
							throw err;
						}
					}

					if (path === '/api/chunk' && method === 'GET') {
						const filePath = url.searchParams.get('path') ?? '';
						const startLine = clampInt(url.searchParams.get('start'), 1, 1, 1_000_000);
						const endLine = clampInt(url.searchParams.get('end'), startLine, startLine, 1_000_000);
						if (!filePath) return json(res, { error: 'path is required' }, 400);
						try {
							const { tools, ctx } = await getSearch();
							const chunk = await tools.readChunk({ path: filePath, startLine, endLine }, ctx);
							return json(res, chunk);
						} catch (err) {
							if (err instanceof SearchUnavailableError) {
								return json(res, { error: 'index-not-built', message: err.message }, 503);
							}
							throw err;
						}
					}

					// ── Index status / maintenance ────────────────────
					if (path === '/api/index/status' && method === 'GET') {
						return json(res, await getIndexStatus(projectRoot));
					}

					if (path === '/api/index/config' && method === 'GET') {
						return json(res, await getIndexConfig(projectRoot));
					}

					if (path === '/api/index/job' && method === 'GET') {
						return json(res, publicJob(currentJob));
					}

					if ((path === '/api/index/refresh' || path === '/api/index/rebuild') && method === 'POST') {
						if (currentJob && currentJob.status === 'running') {
							return json(res, {
								error: 'busy',
								message: `An index ${currentJob.kind} job is already running.`,
								job: publicJob(currentJob),
							}, 409);
						}
						const kind: JobKind = path.endsWith('refresh') ? 'refresh' : 'rebuild';
						currentJob = startIndexerJob(kind, projectRoot);
						// On rebuild, drop our cached read-only handle so the next
						// search opens against the new DB.
						if (kind === 'rebuild') closeSearch();
						return json(res, publicJob(currentJob), 202);
					}

					// ── Existing endpoints ────────────────────────────
					let match = path.match(/^\/api\/stages\/([^/]+)$/);
					if (match) {
						const stage = decodeURIComponent(match[1]);
						if (!STAGES.includes(stage as typeof STAGES[number])) {
							return json(res, { error: 'Invalid stage' }, 400);
						}
						const tickets = await getStage(stage);
						tickets.sort((a, b) => {
							const seqDiff = (a.sequence ?? Infinity) - (b.sequence ?? Infinity);
							return seqDiff !== 0 ? seqDiff : a.slug.localeCompare(b.slug);
						});
						return json(res, tickets);
					}

					match = path.match(/^\/api\/tickets\/([^/]+)\/([^/]+)$/);
					if (match) {
						const stage = decodeURIComponent(match[1]);
						const filename = decodeURIComponent(match[2]);
						return json(res, await getTicket(stage, filename));
					}

					json(res, { error: 'Not found' }, 404);
				} catch (err: any) {
					console.error('[tess-api]', err);
					json(res, { error: err.message }, 500);
				}
			});
		},
	};
}

function clampInt(raw: string | null, fallback: number, min: number, max: number): number {
	if (raw === null) return fallback;
	const n = Number(raw);
	if (!Number.isFinite(n)) return fallback;
	return Math.max(min, Math.min(max, Math.floor(n)));
}
