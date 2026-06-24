/**
 * Claude adapter — invokes `claude` with stream-json output.
 *
 * On Windows we spawn through a shell so npm shims (.cmd/.ps1) resolve; on
 * POSIX we exec the binary directly.
 *
 * When a token budget is configured, the adapter registers a PreToolUse hook
 * (lib/budget-hook.mjs) via a temp `--settings` file.  The runner writes the
 * warning to the file named by TESS_BUDGET_FLAG_FILE once the soft budget is
 * crossed; the hook injects it into the model's next turn.  We write to a
 * file rather than passing JSON inline so the cross-platform shell-quoting
 * rules don't bite — `--settings <path>` is one path argument.
 */

import { writeFile, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { resolveModelEffort } from '../model-selection.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HOOK_SCRIPT = join(__dirname, '..', 'budget-hook.mjs');

// `claude -p` does NOT auto-spawn stdio servers from a project-scoped
// .mcp.json — outside `claude doctor` it treats the file as untrusted, even
// with --dangerously-skip-permissions.  Without --mcp-config the agent can
// see only built-in tools, so any deferred-tool selector for `mcp__<name>__*`
// (e.g. tess's own `code-search`) returns nothing and the agent silently
// falls back to grep/Read.  Pass the file through when it exists so project
// MCP servers are actually loaded.
async function projectMcpConfig(cwd) {
	const path = join(cwd, '.mcp.json');
	try { await access(path, constants.R_OK); return path; }
	catch { return null; }
}

/** Sum of tokens that occupy the model's context window for a given turn. */
function contextSize(usage) {
	if (!usage) return 0;
	return (usage.input_tokens ?? 0)
		+ (usage.cache_read_input_tokens ?? 0)
		+ (usage.cache_creation_input_tokens ?? 0);
}

const DIFF_LINE_CAP = 50;

/** Render `text` as lines each prefixed with `prefix`, capped at `cap` total lines. */
function prefixLines(text, prefix, cap) {
	const lines = String(text ?? '').split('\n');
	if (lines.length <= cap) {
		return lines.map(l => `${prefix}${l}`).join('\n');
	}
	const shown = lines.slice(0, cap).map(l => `${prefix}${l}`).join('\n');
	return `${shown}\n… [+${lines.length - cap} more lines truncated]`;
}

/**
 * Format a tool_use input block for the log. Edit/Write get a +/- diff view
 * showing the new content (and old, for Edit). Everything else falls back to
 * a 200-char JSON snippet.
 */
function formatToolInput(name, input) {
	if (input && typeof input === 'object') {
		if (name === 'Edit' && typeof input.old_string === 'string' && typeof input.new_string === 'string') {
			const header = input.file_path ? `${input.file_path}\n` : '';
			const oldBudget = Math.floor(DIFF_LINE_CAP / 2);
			const newBudget = DIFF_LINE_CAP - oldBudget;
			return `${header}${prefixLines(input.old_string, '- ', oldBudget)}\n${prefixLines(input.new_string, '+ ', newBudget)}`;
		}
		if (name === 'Write' && typeof input.content === 'string') {
			const header = input.file_path ? `${input.file_path}\n` : '';
			return `${header}${prefixLines(input.content, '+ ', DIFF_LINE_CAP)}`;
		}
		return JSON.stringify(input).slice(0, 200);
	}
	return String(input ?? '');
}

/**
 * Format Claude stream-json lines to readable text.
 * Returns { text, done?, usage? } — when done is true the agent has emitted
 * its final result and the runner should stop waiting for a clean exit;
 * `usage` (when present) is the per-turn context-window size in tokens.
 */
function formatStream(line) {
	try {
		const obj = JSON.parse(line);
		if (obj.type === 'system') {
			if (obj.subtype === 'init') {
				// Echo the resolved model so the log records which one actually ran
				// (the runner pins --model from the ticket's difficulty + config, but
				// this confirms the CLI accepted it).
				const model = obj.model ? `  model=${obj.model}` : '';
				return { text: `[session ${obj.session_id ?? '?'}]${model}\n` };
			}
			// thinking_tokens (and any future progress-only system event):
			// collapse to a single dot so the log shows a thinking heartbeat
			// rather than a stream of raw JSON.
			return { text: '.' };
		}
		if (obj.type === 'assistant') {
			const content = obj.message?.content ?? [];
			const parts = [];
			for (const block of content) {
				if (block.type === 'text' && block.text) {
					parts.push(`\n[ASSISTANT]\n${block.text}\n`);
				} else if (block.type === 'tool_use') {
					const inputStr = formatToolInput(block.name, block.input);
					parts.push(`\n[TOOL:${block.name}]\n${inputStr}\n`);
				}
			}
			const usage = obj.message?.usage;
			return { text: parts.join('') || '', usage: usage ? contextSize(usage) : undefined };
		}
		if (obj.type === 'user') {
			const content = obj.message?.content ?? [];
			const parts = [];
			for (const block of content) {
				if (block.type === 'tool_result') {
					const text = Array.isArray(block.content)
						? block.content.map(c => c.text ?? '').join('')
						: String(block.content ?? '');
					const cap = 4000;
					const shown = text.length > cap
						? `${text.slice(0, cap)}\n… [+${text.length - cap} more chars truncated]`
						: text;
					parts.push(`  ✓ ${shown}\n`);
				} else if (block.type === 'text' && block.text) {
					parts.push(`\n[USER]\n${block.text}\n`);
				}
			}
			return { text: parts.join('') || '' };
		}
		if (obj.type === 'result') {
			const status = obj.is_error ? '✗ ERROR' : '✓ DONE';
			const cost = obj.total_cost_usd != null ? ` | cost $${obj.total_cost_usd.toFixed(4)}` : '';
			const dur = obj.duration_ms != null ? ` | ${(obj.duration_ms / 1000).toFixed(1)}s` : '';
			return {
				text: `\n[RESULT ${status}${dur}${cost}]\n${obj.result ?? ''}\n`,
				done: true,
				exitCode: obj.is_error ? 1 : 0,
			};
		}
	} catch {
		/* not JSON, pass through */
	}
	const text = line.endsWith('\n') ? line : line + '\n';
	return { text };
}

/** Settings JSON registering the PreToolUse hook that injects BUDGET_WARNING. */
function buildBudgetSettings() {
	return JSON.stringify({
		hooks: {
			PreToolUse: [
				{
					matcher: '*',
					hooks: [
						{ type: 'command', command: `node "${HOOK_SCRIPT}"` },
					],
				},
			],
		},
	}, null, 2);
}

export async function claude(instructionFile, _prompt, { stage, tokenBudget, cwd, difficulty } = {}) {
	// Difficulty picks the model tier (Fable for `hard`), stage picks the effort
	// (`implement` runs hottest).  Both come from the shared resolver +
	// tess-level config, so the same ticket `difficulty:` drives every adapter.
	const { model, effort } = resolveModelEffort('claude', { stage, difficulty });
	const args = [
		'-p',
		'--dangerously-skip-permissions',
		'--verbose',
		'--no-session-persistence',
		'--output-format', 'stream-json',
	];
	if (model) args.push('--model', model);
	if (effort) args.push('--effort', effort);
	const cleanupFiles = [];
	// `--mcp-config <configs...>` is variadic — commander keeps slurping until
	// the next `--flag`.  Insert it BEFORE another flag (here:
	// --append-system-prompt-file) so it doesn't eat the trailing prompt string.
	if (cwd) {
		const mcpConfig = await projectMcpConfig(cwd);
		if (mcpConfig) args.push('--mcp-config', mcpConfig);
	}
	args.push('--append-system-prompt-file', instructionFile);
	if (Number.isFinite(tokenBudget)) {
		const settingsFile = instructionFile.replace(/\.prompt\.md$/, '.settings.json');
		await writeFile(settingsFile, buildBudgetSettings(), 'utf-8');
		args.push('--settings', settingsFile);
		cleanupFiles.push(settingsFile);
	}
	args.push('Work the ticket as described in the appended system prompt.');
	// On Windows, spawn() with shell:false cannot resolve .cmd/.ps1 shims
	// installed by npm. Use shellCmd so spawn() runs with shell:true instead.
	if (process.platform === 'win32') {
		const escaped = args.map(a => `"${a.replace(/"/g, '\\"')}"`).join(' ');
		return { shellCmd: `claude ${escaped}`, formatStream, cleanupFiles };
	}
	return { cmd: 'claude', args, formatStream, cleanupFiles };
}
