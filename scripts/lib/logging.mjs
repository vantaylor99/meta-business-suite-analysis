/**
 * Per-ticket agent log file management.  Logs live in tickets/.logs/ (git-ignored).
 */

import { mkdir, readdir, stat, unlink } from 'node:fs/promises';
import { basename, join } from 'node:path';

const RETENTION_DAYS = 14;
const RETENTION_COUNT = 50;
const LOG_SUFFIXES = ['.log', '.prompt.md', '.budget-warning'];

/** Return the .logs dir path, ensuring it exists. */
export async function ensureLogsDir(ticketsDir) {
	const logsDir = join(ticketsDir, '.logs');
	await mkdir(logsDir, { recursive: true });
	return logsDir;
}

/** Build a log file path for a ticket run. */
export function logPath(logsDir, ticket) {
	const name = ticket.file.replace(/\.md$/, '');
	const ts = new Date().toISOString().replace(/[:.]/g, '-');
	return join(logsDir, `${name}.${ticket.stage}.${ts}.log`);
}

function groupPrefix(name) {
	for (const s of LOG_SUFFIXES) if (name.endsWith(s)) return name.slice(0, -s.length);
	return null;
}

/**
 * Prune old log groups.  A "group" is a .log file plus its sibling .prompt.md
 * and .budget-warning files (same prefix).  Drops any group whose newest file
 * is older than RETENTION_DAYS, then caps the survivors to RETENTION_COUNT
 * most-recent groups.  The optional protectedLog (typically the prior run's
 * log path from .in-progress) is always kept so a resume note can't dangle.
 */
export async function pruneOldLogs(logsDir, protectedLog = null) {
	let entries;
	try {
		entries = await readdir(logsDir);
	} catch {
		return { removedFiles: 0, removedGroups: 0 };
	}

	const groups = new Map();
	for (const name of entries) {
		const prefix = groupPrefix(name);
		if (!prefix) continue;
		let group = groups.get(prefix);
		if (!group) {
			group = { files: [], mtime: 0 };
			groups.set(prefix, group);
		}
		group.files.push(name);
		try {
			const st = await stat(join(logsDir, name));
			if (st.mtimeMs > group.mtime) group.mtime = st.mtimeMs;
		} catch { /* file vanished mid-scan; ignore */ }
	}

	const protectedPrefix = protectedLog ? groupPrefix(basename(protectedLog)) : null;
	const ageCutoff = Date.now() - RETENTION_DAYS * 24 * 60 * 60 * 1000;
	const sorted = [...groups.entries()].sort((a, b) => b[1].mtime - a[1].mtime);

	let removedFiles = 0;
	let removedGroups = 0;
	for (let i = 0; i < sorted.length; i++) {
		const [prefix, group] = sorted[i];
		if (prefix === protectedPrefix) continue;
		if (group.mtime >= ageCutoff && i < RETENTION_COUNT) continue;
		for (const name of group.files) {
			try {
				await unlink(join(logsDir, name));
				removedFiles++;
			} catch { /* race or permission; skip */ }
		}
		removedGroups++;
	}
	return { removedFiles, removedGroups };
}
