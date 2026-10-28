// Light or dark. Until the user picks one, the system setting decides (see index.css).
export type Theme = "light" | "dark";

const KEY = "icp.theme";

export function initTheme(): void {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  } catch {
    // storage unavailable: follow the system setting
  }
}

export function currentTheme(): Theme {
  const set = document.documentElement.dataset.theme;
  if (set === "light" || set === "dark") return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // the choice just is not remembered
  }
}
