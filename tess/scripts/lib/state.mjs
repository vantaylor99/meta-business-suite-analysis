/**
 * Run-level filesystem state: stop file, in-progress marker, resume notes.
 *
 * The runner uses two sidecar files in tickets/:
 *   - `.stop`        — create to gracefully halt the runner between tickets
 *   - `.in-progress` — written before each ticket, removed on success; lets the
 *                      next run detect an interrupted attempt and prepend a
 *                      resume note to the ticket so the agent picks up where
 *                      it left off.
 */

import { readFile, writeFile, unlink, access } from 'node:fs/promises';
import { join } from 'node:path';
import { constants } from 'node:fs';

const STOP_FILE = '.stop';
const IN_PROGRESS_FILE = '.in-progress';

const RESUME_MARKER_START = '<!-- resume-note -->';
const RESUME_MARKER_END = '<!-- /resume-note -->';

export async function pathExists(p) {
	try { await access(p, constants.F_OK); return true; } catch { return false; }
}

/** Returns true (and removes the stop file) if the user has asked the runner to halt. */
export async function checkStop(ticketsDir) {
	const stopFile = join(ticketsDir, STOP_FILE);
	if (await pathExists(stopFile)) {
		await unlink(stopFile).catch(() => {});
		return true;
	}
	return false;
}

function inProgressPath(ticketsDir) {
	return join(ticketsDir, IN_PROGRESS_FILE);
}

/** Read and clear any prior in-progress state. Returns parsed object or null. */
export async function readAndClearInProgress(ticketsDir) {
	const p = inProgressPath(ticketsDir);
	try {
		const raw = await readFile(p, 'utf-8');
		await unlink(p).catch(() => {});
		return JSON.parse(raw);
	} catch {
		return null;
	}
}

/** Write in-progress state before starting a ticket. */
export async function writeInProgress(ticketsDir, ticket, logFile, agent) {
	const state = {
		file: ticket.file,
		stage: ticket.stage,
		sequence: ticket.sequence,
		slug: ticket.slug,
		path: ticket.path,
		logFile,
		agent,
		startedAt: new Date().toISOString(),
	};
	await writeFile(inProgressPath(ticketsDir), JSON.stringify(state, null, '\t'), 'utf-8');
}

/** Clear in-progress state after successful completion. */
export async function clearInProgress(ticketsDir) {
	await unlink(inProgressPath(ticketsDir)).catch(() => {});
}

function buildResumeNote(priorRun) {
	return [
		RESUME_MARKER_START,
		'RESUME: A prior agent run on this ticket did not complete.',
		`  Prior run: ${priorRun.startedAt} (agent: ${priorRun.agent})`,
		`  Log file: ${priorRun.logFile}`,
		'Read the log to see what was done. Resume where it left off.',
		'If the prior run hit a timeout or repeated error, be cautious not to rush into the same situation.',
		RESUME_MARKER_END,
		'',
	].join('\n');
}

/** Prepend a resume note to a ticket file. Idempotent — replaces any existing note. */
export async function addResumeNote(ticketPath, priorRun) {
	let content = await readFile(ticketPath, 'utf-8');
	const startIdx = content.indexOf(RESUME_MARKER_START);
	const endIdx = content.indexOf(RESUME_MARKER_END);
	if (startIdx !== -1 && endIdx !== -1) {
		content = content.slice(0, startIdx) + content.slice(endIdx + RESUME_MARKER_END.length).replace(/^\n/, '');
	}
	const note = buildResumeNote(priorRun);
	await writeFile(ticketPath, note + content, 'utf-8');
}
