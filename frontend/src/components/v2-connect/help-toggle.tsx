import { useHelpPopover } from "../use-help-popover";

export function useHelpToggle(label: string) {
  return useHelpPopover(label);
}
