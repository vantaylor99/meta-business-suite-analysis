#!/usr/bin/env node
/**
 * Tess code-search CLI — human-facing companion to the MCP server.
 *
 * Queries the same local sqlite-vec index built by `tess/scripts/index.mjs`
 * and shares all ranking/formatting logic with `mcp-search.mjs` via
 * `lib/search-tools.mjs`.
 *
 * Usage:
 *   tess-search "where do we evict pages from the buffer pool"
 *   tess-search -k 5 --path "packages/lamina-substrate/%" "page eviction"
 *   tess-search --refs "composeNewSlot|defaultComposeNewSlot"
 *   tess-search --read packages/lamina/src/index.ts:120-160
 *   tess-search --json "page eviction"
 *
 * Equivalent invocations when not installed as a bin:
 *   node tess/scripts/search.mjs "..."
 *   ./tess/scripts/search.mjs   "..."        (Unix, after chmod +x)
 *   yarn --cwd tess search       "..."
 */

import { resolve } from 'node:path';

import {
	openSearchIndex,
	searchCode,
	findReferences,
	readChunk,
	formatMatches,
	formatReferences,
	formatReadChunk,
	IndexNotBuiltError,
} from './lib/search-tools.mjs';

const HELP = `tess-search — local code search

Modes (pick one; default is semantic search):
  tess-search [opts] <query...>           Semantic search.
  tess-search --refs <symbol>             Literal substring; "|" ORs alternatives.
  tess-search --read <path>:<start>-<end> Read a line range from a file.

Options:
  -k <n>             Max semantic matches (default 5, capped at 50).
  -m, --max <n>      Max literal matches  (default 50, capped at 500).
  -p, --path <like>  SQL LIKE path filter, e.g. "packages/lamina/%".
      --project <d>  Project root (default: cwd; or $TESS_PROJECT_ROOT).
      --json         Emit JSON instead of formatted text.
  -h, --help         Show this help.

Exit codes: 0 on hits, 1 on no hits, 2 on usage / index errors.
`;

function parseArgs(argv) {
	const opts = {
		mode: 'search',           // 'search' | 'refs' | 'read'
		query: null,
		symbol: null,
		readSpec: null,           // { path, startLine, endLine }
		k: 5,
		max: 50,
		pathFilter: null,
		repoRoot: process.cwd(),
		json: false,
		help: false,
	};
	const positional = [];

	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		const next = () => {
			if (i + 1 >= argv.length) usageError(`${a} requires a value`);
			return argv[++i];
		};
		switch (a) {
			case '-h': case '--help':       opts.help = true; break;
			case '--json':                  opts.json = true; break;
			case '-k':                      opts.k = parseIntArg(a, next()); break;
			case '-m': case '--max':        opts.max = parseIntArg(a, next()); break;
			case '-p': case '--path':       opts.pathFilter = next(); break;
			case '--project':               opts.repoRoot = resolve(next()); break;
			case '--refs':                  opts.mode = 'refs';  opts.symbol  = next(); break;
			case '--read':                  opts.mode = 'read';  opts.readSpec = parseReadSpec(next()); break;
			default:
				if (a.startsWith('-')) usageError(`unknown option: ${a}`);
				positional.push(a);
		}
	}

	if (process.env.TESS_PROJECT_ROOT) opts.repoRoot = resolve(process.env.TESS_PROJECT_ROOT);

	if (opts.mode === 'search' && !opts.help) {
		if (positional.length === 0) usageError('a query is required (or pass --refs / --read / --help)');
		opts.query = positional.join(' ');
	} else if (positional.length > 0) {
		usageError(`unexpected positional argument: ${positional[0]}`);
	}

	return opts;
}

function parseIntArg(flag, raw) {
	const n = Number(raw);
	if (!Number.isFinite(n) || Math.floor(n) !== n || n < 1) {
		usageError(`${flag} expects a positive integer, got "${raw}"`);
	}
	return n;
}

// "<path>:<start>-<end>" or "<path>:<start>" (single line).
// On Windows, paths can contain a drive-letter colon ("C:\foo").  Match the
// trailing line-range from the right so we don't split on the drive colon.
function parseReadSpec(raw) {
	const m = /^(.+):(\d+)(?:-(\d+))?$/.exec(raw);
	if (!m) usageError(`--read expects "<path>:<start>[-<end>]", got "${raw}"`);
	const start = Number(m[2]);
	const end = m[3] !== undefined ? Number(m[3]) : start;
	if (end < start) usageError(`--read end (${end}) is before start (${start})`);
	return { path: m[1], startLine: start, endLine: end };
}

function usageError(msg) {
	process.stderr.write(`tess-search: ${msg}\n\nRun 'tess-search --help' for usage.\n`);
	process.exit(2);
}

async function main() {
	const opts = parseArgs(process.argv.slice(2));
	if (opts.help) {
		process.stdout.write(HELP);
		process.exit(0);
	}

	let ctx;
	try {
		ctx = await openSearchIndex({ repoRoot: opts.repoRoot });
	} catch (err) {
		if (err instanceof IndexNotBuiltError) {
			process.stderr.write(`${err.message}\n`);
			process.exit(2);
		}
		throw err;
	}

	if (opts.mode === 'search') {
		const matches = await searchCode({
			query: opts.query,
			k: opts.k,
			pathFilter: opts.pathFilter,
		}, ctx);
		emit(opts.json ? jsonStringify({ mode: 'search', query: opts.query, matches })
			: formatMatches(matches));
		process.exit(matches.length === 0 ? 1 : 0);
	}

	if (opts.mode === 'refs') {
		const rows = findReferences({
			symbol: opts.symbol,
			max: opts.max,
			pathFilter: opts.pathFilter,
		}, ctx);
		emit(opts.json ? jsonStringify({ mode: 'refs', symbol: opts.symbol, rows })
			: formatReferences(opts.symbol, rows));
		process.exit(rows.length === 0 ? 1 : 0);
	}

	if (opts.mode === 'read') {
		const chunk = await readChunk(opts.readSpec, ctx);
		emit(opts.json ? jsonStringify({ mode: 'read', ...chunk })
			: formatReadChunk(chunk));
		process.exit(0);
	}
}

function emit(text) {
	process.stdout.write(text.endsWith('\n') ? text : text + '\n');
}

function jsonStringify(obj) {
	return JSON.stringify(obj, null, 2);
}

main().catch(err => {
	process.stderr.write(`tess-search: ${err.stack ?? err.message ?? String(err)}\n`);
	process.exit(2);
});
