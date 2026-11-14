import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import prettier from "eslint-config-prettier/flat";
import tseslint from "typescript-eslint";
import globals from "globals";
import hooks from "eslint-plugin-react-hooks";
import refresh from "eslint-plugin-react-refresh";
import i18n from "./.eslint/local-i18n-plugin.ts";

export default defineConfig([
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/dist-runtime/**",
      "**/build/**",
      "**/.tmp/**",
      "tests/**",
      "**/*.test.*",
      "**/*.spec.*",
    ],
  },
  {
    files: ["**/*.{js,mjs,cjs,jsx}"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node, parserOptions: { ecmaFeatures: { jsx: true } } },
  },
  {
    files: ["**/*.{ts,mts,cts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    rules: {
      // Existing callback placeholders and destructuring omissions use an underscore.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["*.ts", ".eslint/**/*.ts"],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": hooks, "react-refresh": refresh },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-hooks/set-state-in-effect": "error",
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
    },
  },
  {
    files: [
      "src/App.tsx",
      "src/main.tsx",
      "src/ui.tsx",
      "src/pages/**/*.{ts,tsx}",
      "src/components/**/*.{ts,tsx}",
    ],
    plugins: { "local-i18n": i18n },
    rules: { "local-i18n/no-hardcoded-ui-strings": "error" },
  },
  {
    files: ["src/provider-workspace-data.ts"],
    plugins: { "local-i18n": i18n },
    rules: { "local-i18n/no-hardcoded-data-copy": "error" },
  },
  prettier,
]);
