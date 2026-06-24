/**
 * Strategy registry.
 *
 * A strategy decides *which ticket runs next*.  All strategies share the same
 * agent invocation, logging, and commit pipeline; they differ in how they
 * choose the next ticket:
 *
 *   live   — re-discover and re-prioritize the whole board after every
 *            transition (default). Tickets created mid-run are picked up and
 *            re-ranked immediately; the runner always works the live
 *            highest-priority ticket. Not snapshot-bound.
 *   batch  — drain a startup snapshot stage-by-stage in topo/sequence order
 *            (one stage transition per ticket per run; original behavior).
 *   chase  — pick one root ticket from the snapshot and follow it through every
 *            stage to completion before moving to the next root (ticket-major).
 *            Block/backlog landings cascade through the queue via prereq.
 *
 * `live` is the default because it reassesses priorities continuously; `batch`
 * and `chase` traverse the snapshot frozen at startup for a fixed,
 * one-transition-per-ticket diff or focused ticket-major runs respectively.
 */

import * as live from './live.mjs';
import * as batch from './batch.mjs';
import * as chase from './chase.mjs';

export const strategies = { live, batch, chase };
export const KNOWN_STRATEGIES = Object.keys(strategies);
export const DEFAULT_STRATEGY = 'live';
