// Fuzzy search for the dropdowns: substring first, then letters in order ("grmny"), then a typo of up to
// two letters in a word ("gremany"). Accents and case are ignored.
const norm = (s: string) =>
  s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .trim();

function distance(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = row;
  }
  return prev[b.length];
}

// Higher is better; -1 means no match.
function score(label: string, q: string): number {
  const text = norm(label);
  const at = text.indexOf(q);
  if (at >= 0) return 1000 - at - (at === 0 || text[at - 1] === " " ? 0 : 200);

  // initials: "uk" is United Kingdom, "hr" is Human Resources
  const words = text.split(/[^a-z0-9]+/).filter(Boolean);
  if (words.length > 1 && q.length <= words.length && words.map((w) => w[0]).join("").startsWith(q)) return 900;

  let from = 0;
  let gaps = 0;
  let last = -1;
  for (const ch of q) {
    const i = text.indexOf(ch, from);
    if (i < 0) {
      gaps = -1;
      break;
    }
    if (last >= 0) gaps += i - last - 1;
    last = i;
    from = i + 1;
  }
  if (gaps >= 0 && q.length >= 2 && gaps <= q.length * 2) return 500 - gaps;

  if (q.length >= 3) {
    const allowed = q.length >= 6 ? 2 : 1;
    let best = Infinity;
    for (const w of text.split(/[^a-z0-9]+/)) {
      if (!w) continue;
      best = Math.min(best, distance(q, w), distance(q, w.slice(0, q.length)));
    }
    if (best <= allowed) return 300 - best;
  }
  return -1;
}

export function fuzzyFilter<T>(items: T[], query: string, text: (item: T) => string): T[] {
  const q = norm(query);
  if (!q) return items;
  return items
    .map((item) => ({ item, s: score(text(item), q) }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.item);
}
