/**
 * @spool/mcp-client — TS client for the engine's local MCP server (engine/mcp_server.py).
 *
 * Powers the studio's Agent panel. The MCP server is trove's stdio server *extended* with
 * the clip tools (spec §4) — never a parallel implementation. This client forwards tool
 * calls and surfaces server-initiated **elicitation** requests, which the UI renders as
 * inline cards (pick candidates, aspect, pan-vs-split, ROI-confirm, caption-style).
 *
 * Phase 1 implements the transport (stdio in-app, or Streamable HTTP for remote control)
 * and the tool/elicitation plumbing.
 */

/** The human-judgment decision points the agent pauses on (spec §2/§4). */
export type ElicitationKind =
  | "pick-candidates"
  | "aspect"
  | "pan-vs-split"
  | "roi-confirm"
  | "caption-style";

export interface ElicitationRequest {
  id: string;
  kind: ElicitationKind;
  /** Pre-formatted prompt for chat; the card renders structured controls from `payload`. */
  prompt: string;
  payload?: unknown;
}

export interface SpoolMcpOptions {
  /** Phase 1: stdio command/args for the in-app server, or an HTTP endpoint for remote. */
  command?: string;
  args?: string[];
  url?: string;
}

export class SpoolMcpClient {
  constructor(private readonly opts: SpoolMcpOptions = {}) {}

  // Phase 1: connect(), listTools(), callTool(name, args), onElicitation(cb),
  // respondToElicitation(id, answer), disconnect().
}
