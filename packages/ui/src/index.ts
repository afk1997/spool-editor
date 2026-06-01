/**
 * @spool/ui — the design system.
 *
 * Phase 1 ports the approved demo's design tokens (--bg, --text, --accent, --roi-*,
 * --caption-hl, --radius, --font-*, plus the data-density and data-accent hooks) into a
 * single theme layer (Tailwind theme + CSS variables), then builds the recurring,
 * prop-typed, documented components from the Design Brief (spec §6.3):
 *
 *   MediaCard · CandidateCard + ScoreBar · AspectToggle · ReframeModeToggle · VideoPreview
 *   Timeline · ROIEditor · CaptionStyler · TranscriptView · JobRow / QueueDrawer
 *   AgentPanel · ElicitationCard · CommandPalette · DependencyDoctor
 *   EmptyState / Skeleton / ErrorState
 *
 * Each ships as an isolated unit with a clear prop contract. This entry re-exports them.
 */

export {};
