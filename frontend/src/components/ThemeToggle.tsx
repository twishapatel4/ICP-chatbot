import { useState } from "react";
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
        setTheme(next);
        setCurrent(next);
      }}
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} />
    </button>
  );
}
