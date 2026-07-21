import { SpoolApiError } from "@spool/api-client";

export interface ActionErrorDescription {
  code: string;
  message: string;
}

const ACTION_ERROR_COPY: Readonly<Record<string, string>> = {
  queue_full: "The work queue is full. Wait for a job to finish, then try again.",
  invalid_url: "Enter a valid HTTP or HTTPS URL.",
  origin_forbidden: "That source is blocked by the engine's origin policy.",
  agent_mutation_disabled:
    "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
  offline_network_disabled: "Turn off Offline mode before using this network action.",
  network_work_active: "Wait for active network work to finish before turning on Offline mode.",
  reasoning_provider_required: "Select Codex as the reasoning provider before using this action.",
  egress_consent_required:
    "Allow transcript text to be sent to Codex before using remote reasoning.",
  egress_consent_requires_codex: "Select Codex before granting remote-reasoning consent.",
  settings_persist_failed: "The engine could not save settings. Your confirmed settings were kept.",
  not_resumable: "This job cannot be resumed. Start it again instead.",
  timeout: "The engine took too long to respond. Try again.",
  unreachable: "The engine is unreachable. Make sure it is running, then try again.",
};

function structuredError(error: unknown): { code?: string; message?: string } {
  if (error instanceof SpoolApiError) return { code: error.code, message: error.message };
  if (!error || typeof error !== "object") return {};

  const value = error as { code?: unknown; error?: unknown; message?: unknown };
  const rawCode =
    typeof value.code === "string"
      ? value.code
      : typeof value.error === "string"
        ? value.error
        : undefined;
  return {
    code: rawCode,
    message: typeof value.message === "string" ? value.message : undefined,
  };
}

function isTransportFailure(error: unknown): boolean {
  return (
    error instanceof TypeError &&
    /(?:failed to fetch|fetch failed|network|load failed)/i.test(error.message)
  );
}

/** Turn engine/client failures into actionable UI copy without losing the raw diagnostic code. */
export function describeActionError(error: unknown, fallback?: string): ActionErrorDescription {
  const structured = structuredError(error);
  const code = structured.code || (isTransportFailure(error) ? "unreachable" : "action_failed");
  const message =
    ACTION_ERROR_COPY[code] ?? structured.message ?? fallback ?? "The action failed. Try again.";
  return { code, message };
}

export function formatActionError(error: unknown, fallback?: string): string {
  const { code, message } = describeActionError(error, fallback);
  return `${message} (${code})`;
}

/** Compatibility shape for callers that need an explicitly named raw-code field. */
export function actionError(
  error: unknown,
  fallback?: string,
): ActionErrorDescription & { rawCode: string } {
  const described = describeActionError(error, fallback);
  return { ...described, rawCode: described.code };
}
