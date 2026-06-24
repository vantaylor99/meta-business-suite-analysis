/**
 * Per-agent MCP config writers for the tess search server.
 *
 * Each agent stores its MCP server registry in a different place and format.
 * This module knows the location and shape for each supported agent and
 * merges (never overwrites) the tess entry into the existing config.
 *
 * Agents with no MCP support today (auggie) are tolerated as no-ops with a
 * console hint.
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join, relative, sep, posix } from 'node:path';

const SERVER_NAME = 'code-search';

function toPosix(p) { return p.split(sep).join(posix.sep); }

/**
 * Build the standard MCP server entry: `node tess/scripts/mcp-search.mjs`,
 * cwd-anchored at the project root.
 */
function buildEntry(projectRoot, tessRoot) {
	const script = toPosix(relative(projectRoot, join(tessRoot, 'scripts', 'mcp-search.mjs')));
	return {
		type: 'stdio',
		command: 'node',
		args: [script],
		env: {},
	};
}

async function readJson(path) {
	try { return JSON.parse(await readFile(path, 'utf-8')); }
	catch (err) {
		if (err.code === 'ENOENT') return null;
		throw new Error(`failed to parse ${path}: ${err.message}`);
	}
}

async function writeJson(path, value) {
	await mkdir(dirname(path), { recursive: true });
	await writeFile(path, JSON.stringify(value, null, 2) + '\n', 'utf-8');
}

// ─── Per-agent writers ─────────────────────────────────────────────────────────

async function writeClaude(projectRoot, tessRoot) {
	const path = join(projectRoot, '.mcp.json');
	const existing = await readJson(path) ?? {};
	existing.mcpServers ??= {};
	const before = JSON.stringify(existing.mcpServers[SERVER_NAME]);
	existing.mcpServers[SERVER_NAME] = buildEntry(projectRoot, tessRoot);
	const after = JSON.stringify(existing.mcpServers[SERVER_NAME]);
	if (before === after) return { path, action: 'unchanged' };
	await writeJson(path, existing);
	return { path, action: before === undefined ? 'added' : 'updated' };
}

async function writeCursor(projectRoot, tessRoot) {
	const path = join(projectRoot, '.cursor', 'mcp.json');
	const existing = await readJson(path) ?? {};
	existing.mcpServers ??= {};
	const before = JSON.stringify(existing.mcpServers[SERVER_NAME]);
	existing.mcpServers[SERVER_NAME] = buildEntry(projectRoot, tessRoot);
	const after = JSON.stringify(existing.mcpServers[SERVER_NAME]);
	if (before === after) return { path, action: 'unchanged' };
	await writeJson(path, existing);
	return { path, action: before === undefined ? 'added' : 'updated' };
}

async function writeCodex(projectRoot, tessRoot) {
	// codex-cli reads ~/.codex/config.toml.  We don't write a user-global file
	// from a project init; instead, we drop a project-scoped sample and tell
	// the operator the line to paste.  This avoids surprising mutation of a
	// shared user config.
	const samplePath = join(projectRoot, '.codex', 'mcp-tess.toml.sample');
	const script = toPosix(relative(projectRoot, join(tessRoot, 'scripts', 'mcp-search.mjs')));
	const content = `# Append to ~/.codex/config.toml to enable tess search inside codex-cli:
[mcp_servers.${SERVER_NAME}]
command = "node"
args = ["${script}"]
`;
	await mkdir(dirname(samplePath), { recursive: true });
	await writeFile(samplePath, content, 'utf-8');
	return { path: samplePath, action: 'sample-written' };
}

async function writeAuggie(_projectRoot, _tessRoot) {
	return {
		path: null,
		action: 'unsupported',
		hint: 'auggie does not support MCP today; tess search is a no-op for this agent.',
	};
}

const WRITERS = {
	claude: writeClaude,
	cursor: writeCursor,
	codex: writeCodex,
	auggie: writeAuggie,
};

export const SUPPORTED_AGENTS = Object.keys(WRITERS);

/**
 * Write the MCP config for one agent.  Returns { path, action, hint? }.
 */
export async function writeMcpConfig(agent, projectRoot, tessRoot) {
	const writer = WRITERS[agent];
	if (!writer) throw new Error(`unknown agent: ${agent}`);
	return writer(projectRoot, tessRoot);
}
