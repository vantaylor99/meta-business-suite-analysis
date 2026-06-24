/**
 * Model / effort selection.
 *
 * Tickets carry a portable `difficulty:` (easy | medium | hard, default medium)
 * — an agent-agnostic notion of how much horsepower the work needs.  The
 * concrete model and reasoning-effort are API-specific, so they live here in a
 * per-agent configuration instead of on the ticket: difficulty picks the model
 * tier, the pipeline stage picks the effort, and an optional sparse
 * `overrides[stage][difficulty]` map wins over either for cross-cutting cells
 * (e.g. an easy review that should still run on a strong model).  Each adapter
 * calls
 * `resolveModelEffort(<agentName>, { stage, difficulty })` and translates the
 * result into its own CLI flags, so the same difficulty notion drives every
 * interface (claude, codex, cursor, …) without leaking one agent's vocabulary
 * into another.
 *
 * Defaults below are the fallback; a tess-level `config/agents.json` (shared
 * across every project using this tess checkout) overrides them per agent —
 * deep-merged, so a partial file only restates what it changes.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = join(__dirname, '..', '..', 'config', 'agents.json');

export const DIFFICULTIES = ['easy', 'medium', 'hard'];
export const DEFAULT_DIFFICULTY = 'medium';

/**
 * Built-in fallback, used when `config/agents.json` is absent or omits an
 * agent.  Difficulty → model tier (Fable reserved for the hardest work);
 * effort is per-stage with `implement` bumped one notch above the `default`
 * because it does the most synthesis.  `overrides[stage][difficulty]` pins a
 * specific cell's model and/or effort regardless of those base rules (here: an
 * easy review still runs on Opus, but at reduced effort).  Agents without an
 * entry here resolve to `{ model: null, effort: null }`, i.e. "pass no flags —
 * use the agent's own default model/effort."
 */
const BUILTIN_CONFIG = {
	claude: {
		model: {
			easy: 'claude-sonnet-4-6',
			medium: 'claude-opus-4-8',
			hard: 'claude-opus-4-8',
		},
		effort: {
			implement: 'xhigh',
			default: 'high',
		},
		overrides: {
			review: { easy: { model: 'claude-opus-4-8', effort: 'medium' } },
		},
	},
};

/** Normalize a raw `difficulty:` value to a known token, defaulting to medium. */
export function normalizeDifficulty(value) {
	if (!value) return DEFAULT_DIFFICULTY;
	const v = String(value).trim().toLowerCase();
	return DIFFICULTIES.includes(v) ? v : DEFAULT_DIFFICULTY;
}

function isPlainObject(x) {
	return x != null && typeof x === 'object' && !Array.isArray(x);
}

/** Recursively merge `override` onto `base`: objects merge key-wise, scalars replace. */
function deepMerge(base, override) {
	if (!isPlainObject(base) || !isPlainObject(override)) return override;
	const out = { ...base };
	for (const key of Object.keys(override)) {
		out[key] = deepMerge(base[key], override[key]);
	}
	return out;
}

/** Per-agent deep merge (file wins); `_`-prefixed keys in the file are ignored. */
function mergeConfig(base, override) {
	const merged = {};
	const agents = new Set([
		...Object.keys(base),
		...Object.keys(override).filter(k => !k.startsWith('_')),
	]);
	for (const agent of agents) {
		merged[agent] = deepMerge(base[agent] ?? {}, override[agent] ?? {});
	}
	return merged;
}

let cached = null;
function loadConfig() {
	if (cached) return cached;
	let fileConfig = {};
	try {
		fileConfig = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'));
	} catch (err) {
		if (err.code !== 'ENOENT') {
			console.warn(`tess: ignoring ${CONFIG_PATH}: ${err.message} — using built-in model/effort defaults`);
		}
	}
	cached = mergeConfig(BUILTIN_CONFIG, fileConfig);
	return cached;
}

/**
 * Resolve `{ model, effort }` for an agent from (stage, difficulty).  Either
 * field is `null` when the config doesn't specify one for this agent — the
 * caller should then omit the corresponding CLI flag and let the agent use its
 * own default.  Base rule: difficulty selects the model, stage selects the
 * effort (falling back to the `default` effort key when the stage isn't
 * listed).  A matching `overrides[stage][difficulty]` entry wins per-field, so
 * a single cell can pin a model and/or effort that the base rules wouldn't.
 */
export function resolveModelEffort(agent, { stage, difficulty } = {}) {
	const cfg = loadConfig()[agent] ?? {};
	const diff = normalizeDifficulty(difficulty);
	const override = cfg.overrides?.[stage]?.[diff] ?? {};
	const model = override.model ?? cfg.model?.[diff] ?? null;
	const effort = override.effort ?? cfg.effort?.[stage] ?? cfg.effort?.default ?? null;
	return { model, effort };
}
