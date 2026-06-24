/**
 * Agent process invocation.
 *
 * Spawns the chosen agent adapter, tees stdout/stderr to a log file, and
 * applies an idle-timeout watchdog.  When an agent emits a "done" stream
 * record but doesn't exit promptly, the watchdog force-kills the process
 * tree so the runner doesn't hang.
 *
 * Soft token budget: when `tokenBudget` is set, the runner watches the
 * context-window size reported on each assistant turn (via the adapter's
 * `formatStream`).  Once it crosses the threshold it writes a one-shot
 * BUDGET_WARNING to a flag file; the agent's PreToolUse hook (set up by
 * the adapter) injects that file's contents into the model's next turn.
 * This is a soft signal — the agent decides what to do; the workflow rules
 * tell it to split residual work into continuation tickets.
 */

import { spawn, execSync } from 'node:child_process';
import { writeFile, unlink } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import { agents } from './agents/index.mjs';

export const IDLE_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes with no output → assume hung
export const MAX_TIMEOUT_RETRIES = 1;          // retry a ticket once on idle timeout before moving on

const BUDGET_WARNING_TEXT =
	'BUDGET_WARNING: This run has crossed the configured soft token budget. ' +
	'Per the BUDGET_WARNING section of the ticket workflow rules, stop further ' +
	'investigation now. Capture remaining TODOs as one or more continuation ' +
	'tickets in the SAME stage, delete the source ticket, and exit cleanly.';

/**
 * Force-kill a child process and all its descendants.
 *
 * On Windows we spawn agents with `shell: true`, which means `child` is
 * `cmd.exe` wrapping the actual agent (often a Node process behind a `.cmd`
 * shim). A plain `child.kill()` only terminates cmd.exe — the agent is
 * orphaned, keeps running, and may hold log/prompt files or pipes open.
 * `taskkill /T /F` walks the process tree and force-kills every descendant.
 * On POSIX, `child.kill('SIGKILL')` is sufficient because the runner does
 * not detach into its own process group.
 */
function killTree(child) {
	if (!child || child.killed || child.exitCode != null) return;
	if (process.platform === 'win32') {
		try {
			execSync(`taskkill /pid ${child.pid} /T /F`, { stdio: 'ignore' });
		} catch {
			try { child.kill('SIGKILL'); } catch { /* already gone */ }
		}
	} else {
		try { child.kill('SIGKILL'); } catch { /* already gone */ }
	}
}

/** Write prompt to a temp instruction file, spawn the agent, tee output to log. Returns { exitCode, timedOut, budgetTriggered }. */
export async function runAgent(agentName, prompt, cwd, logFile, { stage, tokenBudget, difficulty } = {}) {
	const adapter = agents[agentName];
	if (!adapter) {
		console.error(`Unknown agent: ${agentName}. Available: ${Object.keys(agents).join(', ')}`);
		process.exit(1);
	}

	const instructionFile = logFile.replace(/\.log$/, '.prompt.md');
	await writeFile(instructionFile, prompt, 'utf-8');

	const budgetFlagFile = Number.isFinite(tokenBudget)
		? logFile.replace(/\.log$/, '.budget-warning')
		: null;

	const adapterResult = await adapter(instructionFile, prompt, { cwd, stage, tokenBudget, difficulty });
	const logStream = createWriteStream(logFile, { flags: 'a' });
	const { cmd, args, shellCmd, formatStream, cleanupFiles = [] } = adapterResult;

	const childEnv = budgetFlagFile
		? { ...process.env, TESS_BUDGET_FLAG_FILE: budgetFlagFile }
		: process.env;

	const spawnArgs = shellCmd
		? [shellCmd, [], { cwd, stdio: ['ignore', 'pipe', 'pipe'], shell: true, env: childEnv }]
		: [cmd, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'], shell: false, env: childEnv }];

	let budgetTriggered = false;

	try {
		return await new Promise((resolve, reject) => {
			const child = spawn(...spawnArgs);
			let idleTimer = null;
			let resultExitCode = null;
			let settled = false;
			let timedOut = false;

			function settle(code) {
				if (settled) return;
				settled = true;
				clearTimeout(idleTimer);
				logStream.end(`\n[runner] Agent exited with code ${code}\n`);
				const done = () => resolve({ exitCode: code, timedOut, budgetTriggered });
				logStream.once('finish', done);
				logStream.once('error', done);
			}

			function resetIdleTimer() {
				if (idleTimer) clearTimeout(idleTimer);
				idleTimer = setTimeout(() => {
					timedOut = true;
					const msg = `\n[runner] Agent idle for ${IDLE_TIMEOUT_MS / 60000}min — killing as hung.\n`;
					process.stderr.write(msg);
					logStream.write(msg);
					killTree(child);
				}, IDLE_TIMEOUT_MS);
			}

			resetIdleTimer();

			function writeOut(text) {
				process.stdout.write(text);
				if (!logStream.write(text)) {
					child.stdout.pause();
					logStream.once('drain', () => child.stdout.resume());
				}
			}

			async function maybeFireBudget(usage) {
				if (budgetTriggered || !budgetFlagFile || !Number.isFinite(tokenBudget)) return;
				if (usage < tokenBudget) return;
				budgetTriggered = true;
				const msg = `\n[runner] Token context ${usage} crossed budget ${tokenBudget} — injecting BUDGET_WARNING.\n`;
				process.stderr.write(msg);
				logStream.write(msg);
				try {
					await writeFile(budgetFlagFile, BUDGET_WARNING_TEXT, 'utf-8');
				} catch (err) {
					const errMsg = `\n[runner] Failed to write budget flag: ${err.message}\n`;
					process.stderr.write(errMsg);
					logStream.write(errMsg);
				}
			}

			function processLine(line) {
				if (!formatStream) { writeOut(line + '\n'); return; }
				const result = formatStream(line);
				if (result.text) writeOut(result.text);
				if (typeof result.usage === 'number') maybeFireBudget(result.usage);
				if (result.done) {
					resultExitCode = result.exitCode ?? 0;
					clearTimeout(idleTimer);
					// Tree-kill on the `result` message rather than waiting for a
					// graceful exit. On Windows, Claude sometimes leaves MCP server
					// children (chrome-devtools-mcp, playwright-mcp) running after a
					// clean exit, leaking ~150 MB each across many ticket runs and
					// eventually starving the system enough that the VS Code pty host
					// crashes and every terminal disconnects. taskkill /T /F walks
					// the descendants and reaps them while the parent PID is still
					// valid.
					killTree(child);
				}
			}

			let buf = '';
			child.stdout.on('data', (chunk) => {
				if (resultExitCode == null) resetIdleTimer();
				buf += chunk.toString();
				const lines = buf.split('\n');
				buf = lines.pop() ?? '';
				for (const line of lines) processLine(line);
			});

			child.stderr.on('data', (chunk) => {
				if (resultExitCode == null) resetIdleTimer();
				process.stderr.write(chunk);
				logStream.write(chunk);
			});

			child.on('error', (err) => {
				const label = shellCmd ? 'agent' : cmd;
				console.error(`Failed to spawn ${label}: ${err.message}`);
				logStream.end(`\n[runner] Agent spawn error: ${err.message}\n`);
				logStream.once('finish', () => reject(err));
				logStream.once('error', () => reject(err));
			});

			child.on('close', (code) => {
				if (buf) processLine(buf.trimEnd());
				settle(resultExitCode ?? code ?? 1);
			});
		});
	} finally {
		process.stdout.write('\x1b[0m');
		await unlink(instructionFile).catch(() => {});
		for (const f of cleanupFiles) await unlink(f).catch(() => {});
		// Hook deletes the flag file on first fire; clean up any straggler
		// (e.g. budget crossed but agent exited before its next tool call).
		if (budgetFlagFile) await unlink(budgetFlagFile).catch(() => {});
	}
}
