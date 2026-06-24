/**
 * Shared search primitives used by both the MCP server (`mcp-search.mjs`)
 * and the CLI (`search.mjs`).
 *
 * Keeping these in one place ensures the agent-facing tool and the human-
 * facing tool can never drift on ranking, formatting, or path resolution.
 *
 *   openSearchIndex({ repoRoot })   → { store, ensureEmbedder, indexDir, repoRoot }
 *   searchCode({ query, k, pathFilter }, ctx)        → matches[]
 *   findReferences({ symbol, max, pathFilter }, ctx) → rows[]
 *   readChunk({ path, startLine, endLine }, ctx)     → { path, start, end, text }
 *
 *   formatMatches(matches)        → string
 *   formatReferences(symbol, rows) → string
 *   formatReadChunk(chunk)         → string
 */

import { join, resolve, isAbsolute, relative, sep, posix } from 'node:path';
import { readFile, access } from 'node:fs/promises';
import { constants } from 'node:fs';

import { IndexStore } from './index-store.mjs';
import { Embedder, DEFAULT_MODEL, DEFAULT_DIM } from './embedder.mjs';

export class IndexNotBuiltError extends Error {
	constructor(dbPath) {
		super(
			`tess code-search: no index at ${dbPath}\n` +
			`Run:  node tess/scripts/index.mjs\n` +
			`from your project root to build it.`,
		);
		this.dbPath = dbPath;
		this.code = 'INDEX_NOT_BUILT';
	}
}

export async function openSearchIndex({ repoRoot }) {
	const indexDir = join(repoRoot, 'tickets', '.index');
	const dbPath = join(indexDir, 'index.db');
	const modelCacheDir = join(indexDir, 'models');

	try { await access(dbPath, constants.R_OK); }
	catch { throw new IndexNotBuiltError(dbPath); }

	const store = await IndexStore.open(dbPath, {
		dim: DEFAULT_DIM,
		modelId: DEFAULT_MODEL,
		readonly: true,
	});

	let embedder = null;
	const ensureEmbedder = async () => {
		if (!embedder) {
			embedder = await Embedder.load(
				modelCacheDir,
				store.getMeta('model_id') ?? DEFAULT_MODEL,
			);
		}
		return embedder;
	};

	return { store, ensureEmbedder, indexDir, dbPath, repoRoot };
}

export async function searchCode({ query, k = 5, pathFilter = null }, ctx) {
	const q = String(query ?? '').trim();
	if (!q) throw new Error('query is required');
	const kClamped = Math.max(1, Math.min(50, Number(k)));
	const embedder = await ctx.ensureEmbedder();
	const [embedding] = await embedder.embed([q], ctx.store.dim);
	return ctx.store.knn(embedding, kClamped, pathFilter);
}

export function findReferences({ symbol, max = 50, pathFilter = null }, ctx) {
	const s = String(symbol ?? '');
	if (!s) throw new Error('symbol is required');
	const maxClamped = Math.max(1, Math.min(500, Number(max)));
	return ctx.store.grepLiteral(s, maxClamped, pathFilter);
}

export async function readChunk({ path, startLine, endLine }, ctx) {
	const reqPath = String(path ?? '');
	if (!reqPath) throw new Error('path is required');
	const start = Math.max(1, Number(startLine ?? 1));
	const end = Math.max(start, Number(endLine ?? start));

	const abs = isAbsolute(reqPath) ? reqPath : join(ctx.repoRoot, reqPath);
	const rel = relative(ctx.repoRoot, resolve(abs));
	if (rel.startsWith('..')) throw new Error('path escapes project root');

	const text = await readFile(abs, 'utf-8');
	const lines = text.split(/\r?\n/);
	const slice = lines.slice(start - 1, end);
	const normalized = rel.split(sep).join(posix.sep);
	return {
		path: normalized,
		start,
		end: start + slice.length - 1,
		text: slice.join('\n'),
	};
}

// ─── Formatting ────────────────────────────────────────────────────────────────

// Cosine-similarity scores from sqlite-vec KNN over a large code corpus
// typically land in the 0.0-0.3 band even for excellent matches — the model
// has to discriminate among thousands of code chunks, which compresses the
// distribution well below the 0.7+ scores seen in isolated 2-way tests.
// Showing the raw cosine misleads readers: 0.16 looks like noise but is
// actually a strong relative match.  We calibrate two ways:
//
//   - WEAK_TOP: if the best score in the result set is below this floor,
//     prepend a "no strong matches" warning.
//   - Per-result confidence: each hit is rendered as a percentage of the
//     top hit's score (top = 100%, lower = relative weakness within the
//     same query).
const WEAK_TOP = 0.05;
const SNIPPET_MAX_LINES = 60;

export function formatMatches(matches) {
	if (matches.length === 0) return 'No matches.';
	const top = matches[0].score;
	const header = top < WEAK_TOP
		? `Top score is weak (raw cosine ${top.toFixed(3)}). Results below may be noise — consider rephrasing the query or falling back to grep.\n\n`
		: '';
	const body = matches.map((m, i) => {
		const tag = i === 0
			? '(top match)'
			: top > 0
				? `(${Math.round((m.score / top) * 100)}% of top)`
				: '(weak)';
		return `[${i + 1}] ${tag}  ${m.path}:${m.start_line}-${m.end_line}\n${trimSnippet(m.text)}`;
	}).join('\n\n---\n\n');
	return header + body;
}

export function formatReferences(symbol, rows) {
	if (rows.length === 0) return `No matches for "${symbol}".`;
	return rows
		.map(r => `${r.path}:${r.start_line}-${r.end_line}\n${trimSnippet(r.text)}`)
		.join('\n\n---\n\n');
}

export function formatReadChunk(chunk) {
	return `${chunk.path}:${chunk.start}-${chunk.end}\n${chunk.text}`;
}

function trimSnippet(text) {
	const lines = text.split('\n');
	if (lines.length <= SNIPPET_MAX_LINES) return text;
	return lines.slice(0, SNIPPET_MAX_LINES).join('\n')
		+ `\n… (${lines.length - SNIPPET_MAX_LINES} more line(s))`;
}
