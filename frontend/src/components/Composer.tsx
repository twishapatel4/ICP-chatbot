import { useEffect, useRef, useState } from "react";

const MAX_CHARS = 2000;

interface Props {
  disabled: boolean;
  hasPicks?: boolean; // chips are selected, so an empty text box can still send
  onSend: (text: string) => void;
}

export default function Composer({ disabled, hasPicks = false, onSend }: Props) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!disabled) ref.current?.focus();
  }, [disabled]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);

  function send() {
    const t = text.trim();
    if ((!t && !hasPicks) || disabled) return;
    setText("");
    onSend(t);
  }

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        ref={ref}
        rows={1}
        maxLength={MAX_CHARS}
        placeholder={hasPicks ? "Add anything else (optional)" : "Type your answer"}
        disabled={disabled}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
      />
      <button className="primary" type="submit" disabled={disabled || (!text.trim() && !hasPicks)}>
        Send
      </button>
    </form>
  );
}
