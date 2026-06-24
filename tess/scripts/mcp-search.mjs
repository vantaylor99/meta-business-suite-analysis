#!/usr/bin/env node
/**
 * Tess code-search MCP server (stdio).
 *
 * Exposes three tools to the agent against the local sqlite-vec index built
 * by `tess/scripts/index.mjs`:
 *
 *   search_code({ query, k?, path_filter? })
 *     → top-k semantic matches with file/line/snippet/score.
 *
 *   find_references({ symbol, max?, path_filter? })
 *     → literal-string matches from the indexed corpus.
 *
 *   read_chunk({ path, start_line, end_line })
 *     → raw text of an arbitrary line range, sourced from disk so it can
 *       expand a snippet returned by search_code.
 *
 * The server refuses to start if no index exists; the error message points
 * at the indexer.
 *
 * The actual search/format logic lives in `lib/search-tools.mjs` and is
 * shared with the human-facing CLI (`scripts/search.mjs`).
 */

// Critical: stdout is the MCP transport.  Any stray write breaks the JSON-RPC
// stream.  Send everything advisory to stderr.
const _origLog = console.log;
console.log = (...args) => console.error(...args);

import { resolve } from 'node:path';

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

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

function parseArgs(argv) {
	const opts = { repoRoot: process.cwd() };
	for (let i = 0; i < argv.length; i++) {
		if (argv[i] === '--project' && argv[i + 1]) opts.repoRoot = resolve(argv[++i]);
	}
	if (process.env.TESS_PROJECT_ROOT) opts.repoRoot = resolve(process.env.TESS_PROJECT_ROOT);
	return opts;
}

const TOOLS = [
	{
		name: 'search_code',
		description: 'Semantic search over the project codebase. Returns ranked code snippets with file paths and line ranges. Best for "where is X used", "what handles Y", "find similar logic to Z" — questions where you do not know the exact identifier.',
		inputSchema: {
			type: 'object',
			properties: {
				query: { type: 'string', description: 'Natural-language description of what to find.' },
				k: { type: 'integer', description: 'Number of matches to return (default 5; raise for broader sweeps, max 50).', default: 5 },
				path_filter: { type: 'string', description: 'Optional SQL LIKE pattern restricting results to matching paths, e.g. "src/%".' },
			},
			required: ['query'],
		},
	},
	{
		name: 'find_references',
		description: 'Literal-string search over the indexed corpus. Use when you have an exact identifier and want every occurrence. Multiple terms separated by "|" are OR-ed (each side is still a literal substring, not a regex) — e.g. "composeNewSlot|defaultComposeNewSlot".',
		inputSchema: {
			type: 'object',
			properties: {
				symbol: { type: 'string', description: 'Literal substring to find. Use "|" to OR multiple alternatives, e.g. "Foo|Bar".' },
				max: { type: 'integer', description: 'Max matches to return (default 50).', default: 50 },
				path_filter: { type: 'string' },
			},
			required: ['symbol'],
		},
	},
	{
		name: 'read_chunk',
		description: 'Read a specific line range from a tracked file. Use to expand a snippet returned by search_code.',
		inputSchema: {
			type: 'object',
			properties: {
				path: { type: 'string', description: 'Project-relative or absolute file path.' },
				start_line: { type: 'integer', description: '1-based start line (inclusive).' },
				end_line: { type: 'integer', description: '1-based end line (inclusive).' },
			},
			required: ['path', 'start_line', 'end_line'],
		},
	},
];

async function main() {
	const opts = parseArgs(process.argv.slice(2));

	let ctx;
	try { ctx = await openSearchIndex({ repoRoot: opts.repoRoot }); }
	catch (err) {
		if (err instanceof IndexNotBuiltError) {
			console.error(`tess-mcp-search: ${err.message}`);
			process.exit(1);
		}
		throw err;
	}

	const server = new Server(
		{ name: 'code-search', version: '0.1.0' },
		{
			capabilities: { tools: {} },
			instructions: [
				'Local semantic + literal search over the project codebase, backed by a',
				'sqlite-vec index built by tess.  This server does NOT search tess tickets,',
				'docs, or chat history — it searches the source files of the host project.',
				'',
				'Use `search_code` for natural-language questions where you do not yet know',
				'the right identifier ("where do we handle JWT refresh", "what enforces',
				'page-cache eviction").  Use `find_references` once you have an exact name.',
				'Use `read_chunk` to expand a snippet returned by either tool.',
				'',
				'Prefer these tools over grep/Glob for exploratory questions about the',
				'codebase; fall back to grep/Glob for exact-string and filename-pattern',
				'lookups.',
			].join('\n'),
		},
	);

	server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

	server.setRequestHandler(CallToolRequestSchema, async (req) => {
		const { name, arguments: args } = req.params;
		try {
			if (name === 'search_code') {
				const matches = await searchCode({
					query: args.query,
					k: args.k,
					pathFilter: args.path_filter ? String(args.path_filter) : null,
				}, ctx);
				return { content: [{ type: 'text', text: formatMatches(matches) }] };
			}
			if (name === 'find_references') {
				const rows = findReferences({
					symbol: args.symbol,
					max: args.max,
					pathFilter: args.path_filter ? String(args.path_filter) : null,
				}, ctx);
				return { content: [{ type: 'text', text: formatReferences(String(args.symbol ?? ''), rows) }] };
			}
			if (name === 'read_chunk') {
				const chunk = await readChunk({
					path: args.path,
					startLine: args.start_line,
					endLine: args.end_line,
				}, ctx);
				return { content: [{ type: 'text', text: formatReadChunk(chunk) }] };
			}
			throw new Error(`unknown tool: ${name}`);
		} catch (err) {
			return {
				isError: true,
				content: [{ type: 'text', text: `error: ${err.message}` }],
			};
		}
	});

	const transport = new StdioServerTransport();
	await server.connect(transport);
	console.error(`code-search ready (index: ${ctx.dbPath})`);
}

main().catch(err => { console.error(err); process.exit(1); });
