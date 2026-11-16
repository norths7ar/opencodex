import type { SizingEffortIntent, SizingTier } from "./sizing-labels";
export interface RoleProposal {
  role: string;
  model: string | null;
  effort: string | null;
  status: "proposed" | "unassigned" | "unsized";
  tier?: SizingTier;
  effortIntent?: SizingEffortIntent;
  rationale?: string;
  moveUpIf?: string;
  moveDownIf?: string;
  proposedModel?: string | null;
  proposedEffort?: string | null;
  reason?: string | null;
}
