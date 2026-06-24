/**
 * Codex adapter — invokes `codex exec --json` (codex-cli ≥ 0.112.0).
 */

import { relative } from 'node:path';
import { resolveModelEffort } from '../model-selection.mjs';

function formatStream(line) {
	try {
		const obj = JSON.parse(line);
		if (obj.type === 'thread.started') {
			return { text: `[session ${obj.thread_id ?? '?'}]\n` };
		}
		if (obj.type === 'turn.started') {
			return { text: '\n[TURN STARTED]\n' };
		}
		if (obj.type === 'item.completed') {
			const item = obj.item ?? {};
			if (item.type === 'agent_message' && item.text) {
				return { text: `\n[ASSISTANT]\n${item.text}\n` };
			}
		}
		if (obj.type === 'turn.completed') {
			const usage = obj.usage ?? {};
			const input = usage.input_tokens != null ? ` in ${usage.input_tokens}` : '';
			const output = usage.output_tokens != null ? ` out ${usage.output_tokens}` : '';
			return {
				text: `\n[RESULT ✓ DONE${input || output ? ` | tokens${input}${output}` : ''}]\n`,
				done: true,
				exitCode: 0,
			};
		}
	} catch {
		/* not JSON, pass through */
	}
	const text = line.endsWith('\n') ? line : line + '\n';
	return { text };
}

export function codex(instructionFile, _prompt, { cwd, stage, difficulty } = {}) {
	const relPath = relative(cwd, instructionFile).replace(/\\/g, '/');
	const prompt = `Read and follow all instructions in the file: ${relPath}`;
	// Inert unless a `codex` block is added to config/agents.json (the built-in
	// defaults cover claude only); `null` values pass no flag.
	const { model, effort } = resolveModelEffort('codex', { stage, difficulty });
	const args = [
		'exec',
		'--json',
		'--color', 'never',
		'--full-auto',
		'--ephemeral',
		'-C', cwd,
	];
	if (model) args.push('-m', model);
	if (effort) args.push('-c', `model_reasoning_effort="${effort}"`);
	args.push(prompt);
	return { cmd: 'codex', args, formatStream };
}
