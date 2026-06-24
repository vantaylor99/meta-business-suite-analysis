/**
 * Agent adapter registry.
 *
 * Each adapter is a function `(instructionFile, prompt, { cwd, stage, difficulty, tokenBudget }) => spec`
 * where spec is one of:
 *   { cmd, args, formatStream? }    — direct exec (no shell)
 *   { shellCmd, formatStream? }     — passed as a single shell string (Windows
 *                                     shims, paths with spaces)
 * `formatStream` is an optional function that converts one line of agent stdout
 * into `{ text, done?, exitCode? }` for the runner's tee + result detection.
 */

import { claude } from './claude.mjs';
import { cursor } from './cursor.mjs';
import { codex } from './codex.mjs';
import { auggie } from './auggie.mjs';

export const agents = { claude, cursor, codex, auggie };
