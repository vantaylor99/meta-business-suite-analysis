/**
 * Augment (auggie) adapter — invokes `auggie --print --instruction <file>`.
 *
 * No structured stream parser yet; runner falls back to passthrough lines.
 */

export function auggie(instructionFile, _prompt) {
	return {
		shellCmd: `auggie --print --instruction "${instructionFile}"`,
	};
}
