"""PYTHONPATH=src python -m icp_chatbot.cli.transcribe_eval eval_audio/manifest.json [--out FILE]

Measures voice transcription quality on real recordings. Each manifest entry names an audio file
(path relative to the manifest), the exact words spoken, and tags to group the results by:

    [{"file": "hi_f_calm_1.wav", "text": "हम कंपनियों को पेरोल सॉफ्टवेयर बेचते हैं",
      "lang": "hi", "accent": "indian", "tone": "calm"}]

Sends each file through the same step the API uses and reports word error rate (WER) and
character error rate (CER) per sample and per lang / accent / tone. Needs a real API key; costs
a few cents per hundred clips. Text is compared after lower-casing and removing punctuation.
Languages written without spaces (Chinese, Japanese, Thai) have no meaningful WER: read CER."""

import argparse
import asyncio
import json
import re
import statistics
import sys
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from icp_chatbot.config import Settings, get_settings
from icp_chatbot.engine.transcribe import normalize_mime, transcribe
from icp_chatbot.llm.factory import make_llm
from icp_chatbot.llm.types import LLM, LLMError

_EXT_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mp3", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".aac": "audio/aac", ".aiff": "audio/aiff",
}  # fmt: skip
_GROUPS = ("lang", "accent", "tone")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(ch if not unicodedata.category(ch).startswith(("P", "S")) else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def error_rate(reference: str, hypothesis: str, *, chars: bool) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    ref_units, hyp_units = (list(ref), list(hyp)) if chars else (ref.split(), hyp.split())
    if not ref_units:
        return 0.0 if not hyp_units else 1.0
    return edit_distance(ref_units, hyp_units) / len(ref_units)


@dataclass
class Sample:
    file: str
    expected: str
    heard: str
    lang: str
    accent: str
    tone: str
    wer: float
    cer: float
    error: str = ""


async def run_eval(llm: LLM, settings: Settings, manifest: Path) -> list[Sample]:
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    out = []
    for e in entries:
        tags = {g: e.get(g, "?") for g in _GROUPS}
        path = manifest.parent / e["file"]
        mime = normalize_mime(_EXT_MIME.get(path.suffix.lower()))
        if mime is None:
            out.append(Sample(e["file"], e["text"], "", **tags, wer=1.0, cer=1.0, error="bad type"))
            continue
        try:
            heard = await transcribe(llm, settings, path.read_bytes(), mime)
        except (LLMError, OSError) as exc:
            out.append(Sample(e["file"], e["text"], "", **tags, wer=1.0, cer=1.0, error=str(exc)))
            continue
        out.append(
            Sample(
                e["file"], e["text"], heard, **tags,
                wer=error_rate(e["text"], heard, chars=False),
                cer=error_rate(e["text"], heard, chars=True),
            )
        )  # fmt: skip
    return out


def summarize(samples: list[Sample]) -> str:
    lines = [f"{len(samples)} clips, {sum(1 for s in samples if s.error)} failed", ""]
    for group in ("all", *_GROUPS):
        buckets: dict[str, list[Sample]] = defaultdict(list)
        for s in samples:
            buckets["all" if group == "all" else getattr(s, group)].append(s)
        lines.append(f"by {group}:" if group != "all" else "overall:")
        for key, rows in sorted(buckets.items()):
            wer = statistics.mean(r.wer for r in rows)
            cer = statistics.mean(r.cer for r in rows)
            lines.append(f"  {key:<14} n={len(rows):<3} WER {wer:6.1%}  CER {cer:6.1%}")
        lines.append("")
    worst = sorted(samples, key=lambda s: s.cer, reverse=True)[:5]
    lines.append("worst clips (by CER):")
    for s in worst:
        lines.append(f"  {s.file}  CER {s.cer:.0%}  {s.error}")
        lines.append(f"    said : {s.expected}")
        lines.append(f"    heard: {s.heard}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, help="write every sample as JSON here")
    args = parser.parse_args(argv)
    settings = get_settings()
    samples = asyncio.run(run_eval(make_llm(settings), settings, args.manifest))
    print(summarize(samples))
    if args.out:
        args.out.write_text(
            json.dumps([asdict(s) for s in samples], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 1 if any(s.error for s in samples) else 0


if __name__ == "__main__":
    sys.exit(main())
