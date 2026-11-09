import React from "react";
import ReactDOM from "react-dom/client";
import { lazy, Suspense } from "react";
// Entry-point component is mounted here, never imported for fast refresh.
const Screen = lazy(() => import("./App"));
import { LanguageProvider } from "./i18n/provider";
import "./styles.css";
import "./styles/usage-chart-accessibility.css";
import "./styles/sidebar-brand.css";
import "./styles/fast-rows-setting.css";
import "./styles/claude-desktop-mode-picker.css";
import "./styles/claude-first-party-bindings.css";
import "./styles/lazycodex-role-models.css";
import "./styles/claude-desktop-picker.css";
import "./styles/anthropic-reset-grants.css";
import "./styles/protocol-evidence.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LanguageProvider>
      <Suspense fallback={null}><Screen /></Suspense>
    </LanguageProvider>
  </React.StrictMode>
);
