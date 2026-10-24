"""Checks that keep the interviewer sounding like a person. The prompt asks for all of this, but
the small live model drifts into long, padded, repetitive messages (2026-10-24: 274 messages
averaged 43 words, 44% opened with a stock line), so the code sends a slip back once."""

import re

MAX_WORDS = 45  # a short reaction plus one question; answering their question fits too
RECENT = 3  # bot messages compared for repeated openings and phrases
PHRASE_WORDS = 4  # a run of this many words repeated from a recent message counts as repetition

STOCK_OPENERS = (
    "that makes sense",
    "it sounds like",
    "i hear you",
    "i understand",
    "i completely understand",
    "i appreciate",
    "great question",
    "absolutely",
)
PRAISE = re.compile(
    r"\b(brilliant|fantastic|excellent|impressive|amazing|awesome|wonderful|spot on|sweet spot|"
    r"powerful|value add|great (insight|point|question|distinction)|love (that|it))\b",
    re.IGNORECASE,
)
QUOTED = re.compile(r"(?<![\w'])['\"‘“]([^'\"‘’“”?]{2,30})['\"’”](?![\w'])")
WORD = re.compile(r"[\w']+")
SENTENCE = re.compile(r"[^.!?;:]+")
# a question may start like the last one did; a lead-in such as "Since you" may not
QUESTION_WORDS = {"what", "who", "which", "how", "when", "where", "why", "do", "does", "did",
                  "is", "are", "can", "could", "would", "will", "have", "has"}  # fmt: skip


def _words(text: str) -> list[str]:
    return WORD.findall(text.lower().replace("’", "'"))


def _phrases(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + PHRASE_WORDS]) for i in range(len(words) - PHRASE_WORDS + 1)}


def _starts(text: str, size: int = 2) -> list[tuple[str, ...]]:
    """The first words of each sentence that is not a plain question."""
    out = []
    for sentence in SENTENCE.findall(text):
        words = _words(sentence)
        if len(words) >= 2 and words[0] not in QUESTION_WORDS:
            out.append(tuple(words[:size]))
    return out


def recent_openings(recent: list[str]) -> list[str]:
    """How the bot's last messages began their sentences, to tell the model up front."""
    seen: list[str] = []
    for message in recent[-RECENT:]:
        for start in _starts(message, 3):
            if (text := " ".join(start)) not in seen:
                seen.append(text)
    return seen


def style_problem(say: str, recent: list[str], user_text: str = "") -> str | None:
    """What makes the message sound robotic, in words for the model, or None if it reads fine.
    recent holds the bot's last messages; user_text is what the user wrote, which may be quoted."""
    words = _words(say)
    opening = " ".join(words[:5])
    if len(words) > MAX_WORDS:
        return f"it has {len(words)} words. Keep it under {MAX_WORDS}: a few words back, then one short question."
    if say.count("?") > 1:
        return "it asks more than one question. Ask one question only."
    for stock in STOCK_OPENERS:
        if opening == stock or opening.startswith(stock + " "):
            return f'it opens with the stock line "{stock}". Open by reacting to what they actually said.'
    if match := PRAISE.search(say):
        return f'it uses praise or an idiom ("{match.group(0)}"). Say it plainly, without judging.'
    for quoted in QUOTED.findall(say):
        if quoted.strip().lower() not in user_text.lower():
            return f"it puts \"{quoted}\" in quotes. Use plain words with no quotes."
    for before in recent[-RECENT:]:
        earlier = _words(before)
        if words[:3] and words[:3] == earlier[:3]:
            return "it starts the same way as one of your last messages. Start differently."
        if repeated := _phrases(words) & _phrases(earlier):
            phrase = " ".join(sorted(repeated)[0])
            return f'it repeats "{phrase}" from one of your last messages. Say it another way or leave it out.'
        if same := [s for s in _starts(say) if s in _starts(before)]:
            return f'it starts a sentence with "{" ".join(same[0])}" again, as one of your last messages did. Start it differently.'
    return None
