import { getActiveLocale } from "../i18n/shared";

export function authStatusCopy() {
  const locale = getActiveLocale();
  return locale === "zh" || locale === "zh-TW"
    ? {
        heading: "登录状态待确认",
        loading: "正在查询登录状态…",
        failed: "登录状态查询失败",
        retained: "登录状态刷新失败，保留上次确认结果。",
      }
    : {
        heading: "Login status unconfirmed",
        loading: "Checking login status…",
        failed: "Could not check login status",
        retained: "Login status refresh failed; showing the last confirmed result.",
      };
}
