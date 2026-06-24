/**
 * Programmatic detection of whether the local code-search MCP server is
 * wired up for this project.  Used by the prompt builder to inject a
 * confident, agent-specific directive only when the tools will actually
 * be available — no more "if available, prefer…" hedging.
 *
 * "Available" means BOTH:
 *   1. An MCP server entry exists in the project's MCP config that points
 *      at `tess/scripts/mcp-search.mjs`.
 *   2. The index DB has been built at `tickets/.index/index.db`.
 *
 * Returns the registered server name (so the prompt can use the right
 * `mcp__<server>__<tool>` namespace) or null if either check fails.
 */

import { readFile, access } from 'node:fs/promises';
import { join } from 'node:path';
import { constants } from 'node:fs';

// Per-agent MCP config locations to probe.  Order matches the writers in
// lib/mcp-config.mjs.  We don't need the agent name — if the project has
// any of these wired, the agent process will see it.
const PROBES = [
	{ path: ['.mcp.json'], shape: 'mcpServers' },
	{ path: ['.cursor', 'mcp.json'], shape: 'mcpServers' },
];

const SERVER_SCRIPT_HINT = 'mcp-search.mjs';

export async function detectSearch(projectRoot) {
	const dbPath = join(projectRoot, 'tickets', '.index', 'index.db');
	try { await access(dbPath, constants.R_OK); }
	catch { return null; }

	for (const probe of PROBES) {
		const cfgPath = join(projectRoot, ...probe.path);
		const cfg = await readJsonOrNull(cfgPath);
		if (!cfg) continue;
		const servers = cfg[probe.shape];
		if (!servers || typeof servers !== 'object') continue;
		for (const [name, entry] of Object.entries(servers)) {
			if (entryPointsAtSearch(entry)) return name;
		}
	}
	return null;
}

function entryPointsAtSearch(entry) {
	if (!entry || typeof entry !== 'object') return false;
	const args = Array.isArray(entry.args) ? entry.args : [];
	return args.some(a => typeof a === 'string' && a.includes(SERVER_SCRIPT_HINT));
}

async function readJsonOrNull(path) {
	try { return JSON.parse(await readFile(path, 'utf-8')); }
	catch { return null; }
}
