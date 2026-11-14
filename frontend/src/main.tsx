import React from "react";
import ReactDOM from "react-dom/client";
import { lazy, Suspense } from "react";
// Entry-point component is mounted here, never imported for fast refresh.
// eslint-disable-next-line react-refresh/only-export-components
const Screen = lazy(() => import("./App"));
import { LanguageProvider } from "./i18n/provider";
// Design system v2 entry: tokens, Tailwind utilities, and every legacy stylesheet in a lower
// `legacy` cascade layer, so migrated components win and unmigrated pages look unchanged.
import "./design/theme.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LanguageProvider>
      <Suspense fallback={null}>
        <Screen />
      </Suspense>
    </LanguageProvider>
  </React.StrictMode>,
);
