import { useI18n } from "../i18n/shared";

/** How a saved local catalog reaches Codex. */
export function ModelCatalogDelivery() {
  const { t } = useI18n();
  const steps = [
    { id: "saved", chip: t("models.delivery.chip.saved"), title: t("models.delivery.saved.title"), body: t("models.delivery.saved.body") },
    { id: "loaded", chip: t("models.delivery.chip.loaded"), title: t("models.delivery.loaded.title"), body: t("models.delivery.loaded.body") },
  ];
  return (
    <details className="models-delivery">
      <summary>
        <span className="models-delivery-title">{t("models.delivery.title")}</span>
        <span className="models-delivery-flow">{steps.map(step => step.chip).join(" → ")}</span>
      </summary>
      <ol className="models-delivery-steps">
        {steps.map(step => (
          <li key={step.id} data-step={step.id}>
            <strong>{step.title}</strong>
            <p>{step.body}</p>
          </li>
        ))}
      </ol>
      <p className="models-delivery-hint">{t("models.delivery.hint")}</p>
    </details>
  );
}
