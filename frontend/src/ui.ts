// Class strings shared by more than one component.

// every screen fills the window, on the page background
export const app = "w-full min-h-screen flex flex-col bg-frame";

// a centred column, as wide as the chat
export const column = "w-full max-w-[1000px] mx-auto";
export const gutter = "px-[clamp(16px,4vw,28px)]";

export const header = "flex items-center justify-between pt-[22px] px-[26px] pb-[10px]";

export const card = "bg-card border border-line rounded-[28px] py-[22px] px-6 self-stretch shadow-soft";

// the pill that holds the answer box, on the welcome screen and in the chat
export const promptBox =
  "flex items-center gap-2 bg-panel min-h-16 border border-line rounded-full py-2 pr-3 pl-[26px] " +
  "shadow-[0_14px_36px_rgba(20,24,31,.08)] transition-[border-color,box-shadow] duration-150 ease-[ease] " +
  "backdrop-blur-[14px] focus-within:border-accent";
export const promptInput =
  "flex-[1_1_0] min-w-0 resize-none min-h-10 max-h-40 text-[14px] py-[9px] px-0 border-0 outline-0 " +
  "bg-transparent text-ink placeholder:text-muted";
export const promptActions = "flex items-center gap-1.5";
export const iconBtn = "size-[42px] p-0 rounded-full justify-center"; // the header's round button
export const roundBtn = `${iconBtn} shadow-none`; // inside the answer pill, where it sits flat
export const sendBtn =
  "bg-accent text-accent-text border-accent disabled:bg-bot disabled:text-muted disabled:border-line disabled:opacity-100";

export const primaryBtn = "bg-accent text-accent-text border-accent";

// one column as wide as the widest option, pushed to the right; every box stretches to it
export const chips = "grid grid-cols-[minmax(0,max-content)] justify-end gap-2";
export const chip = "rounded-[18px] py-2.5 px-4 text-[14px] text-left";
export const chipOn = primaryBtn;

export const actions = "flex gap-2 mt-3.5 flex-wrap";

// the soft rounded text field used in the cards and on the welcome screen
export const pillInput =
  "text-[14px] rounded-full border border-line bg-panel text-ink outline-0 focus:border-accent";
