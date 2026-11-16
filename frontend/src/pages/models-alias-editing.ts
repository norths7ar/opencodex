import { updateChannel } from "@/channel-management";
import type { ApplicationReceipt, ChannelSaved } from "@/contracts/channels";
import { clientApplicationWarning } from "@/contracts/application";
import { readJsonOrThrow } from "@/fetch-json";
import { requestTextValue } from "../action-dialogs";
import type { TFn } from "../i18n/shared";

export interface AliasEditingDeps {
  apiBase: string;
  t: TFn;
  pendingMessage: string;
  /** Re-reads /api/aliases so the row shows what the server now holds. */
  reloadAliases: () => Promise<void>;
  publishFeedback: (ok: boolean, message: string) => void;
}

/** Renames a provider. An empty value clears the alias, as the field label says. */
export async function editProviderAlias(
  provider: string,
  current: string,
  { apiBase, t, reloadAliases, publishFeedback, pendingMessage }: AliasEditingDeps,
): Promise<void> {
  const entered = await requestTextValue({
    message: t("models.aliasPrompt"),
    initialValue: current,
  });
  // Dismissal is not an empty alias: a cancelled edit must write nothing.
  if (entered === null) return;
  try {
    const response = await updateChannel(apiBase, provider, (detail) => ({
      channel: { ...detail.channel, alias: entered.trim() || null },
      credential: { action: "keep" },
    }));
    const receipt = await readJsonOrThrow<ChannelSaved>(response, t("models.aliasConflict"));
    await reloadAliases();
    const warning = clientApplicationWarning(
      receipt?.application,
      t("integrations.catalogSyncFailed"),
    );
    publishFeedback(
      !warning,
      warning ??
        (receipt?.application.status === "pending" ? pendingMessage : t("models.aliasSaved")),
    );
  } catch (error) {
    publishFeedback(false, error instanceof Error ? error.message : t("models.aliasConflict"));
  }
}

/** Renames one model within a provider. An empty value removes the alias. */
export async function editModelAlias(
  provider: string,
  model: string,
  current: string,
  { apiBase, t, reloadAliases, publishFeedback, pendingMessage }: AliasEditingDeps,
): Promise<void> {
  const entered = await requestTextValue({
    message: t("models.modelAliasPrompt"),
    initialValue: current,
  });
  if (entered === null) return;
  const body = entered.trim() ? { set: { [model]: entered.trim() } } : { remove: [model] };
  try {
    const response = await fetch(
      `${apiBase}/api/channels/${encodeURIComponent(provider)}/model-aliases`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    const receipt = await readJsonOrThrow<{ catalogRefresh?: ApplicationReceipt }>(
      response,
      t("models.aliasConflict"),
    );
    await reloadAliases();
    const warning = clientApplicationWarning(
      receipt?.catalogRefresh,
      t("integrations.catalogSyncFailed"),
    );
    publishFeedback(
      !warning,
      warning ??
        (receipt?.catalogRefresh?.status === "pending" ? pendingMessage : t("models.aliasSaved")),
    );
  } catch (error) {
    publishFeedback(false, error instanceof Error ? error.message : t("models.aliasConflict"));
  }
}
