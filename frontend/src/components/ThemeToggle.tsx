import { useState } from "react";
import { flushSync } from "react-dom";
import { currentTheme, setTheme, type Theme } from "../theme";
import Icon from "./Icon";

export default function ThemeToggle() {
  const [theme, setCurrent] = useState<Theme>(currentTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      className="icon-btn"
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      onClick={() => {
        const apply = () => {
          setTheme(next);
          flushSync(() => setCurrent(next));
        };
        // wipe from the old theme to the new one (styles.css); no API or reduced motion: switch at once
        const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (reduce || !document.startViewTransition) apply();
        else document.startViewTransition(apply);
      }}
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} />
    </button>
  );
}
