// Declare cascade layers before the eager shell's component styles are imported.
import "./design/theme.css";
import React from "react";
import ReactDOM from "react-dom/client";
import { LanguageProvider } from "./i18n/provider";
import { preloadInitialLocale } from "./i18n/shared";
import App from "./App";

void preloadInitialLocale();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LanguageProvider
      fallback={
        <div className="console-boot" aria-busy="true">
          {/* Product name is identical in every locale, including before a catalog is ready. */}
          {/* eslint-disable-next-line local-i18n/no-hardcoded-ui-strings */}
          <span>OpenCodex Lite</span>
        </div>
      }
    >
      <App />
    </LanguageProvider>
  </React.StrictMode>,
);
