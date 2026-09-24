"""Corpus definition and structure detection for a regulation-style document.

This is the corpus-heavy reference domain. It exists to prove the RAG path end
to end, and to be the thing someone copies on the day when the scenario is
document-shaped.

The section finder below understands the GDPR's structure specifically. That is
the point of the seam: the core knows nothing about articles or recitals.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentcore.rag.chunking import Section

DOCS = Path(__file__).parent / "docs"
PDF = DOCS / "CELEX_32016R0679_EN_TXT.pdf"

# The '#' is optional because the markdown converter emits most articles as
# '## _Article 7_' but leaves a handful as a bare italic line, '_Article 5_'.
HEADING = re.compile(
    r"^[#\s]*[_*]*\s*(CHAPTER\s+[IVXL]+|Section\s+\d+|Article\s+\d+)\s*[_*]*\s*$",
    re.M,
)

# Recitals are the numbered paragraphs before Article 1: '- (30) Natural persons...'
RECITAL = re.compile(r"^\s*-?\s*\((\d{1,3})\)\s+(?=\S)", re.M)


def is_heading(line: str) -> bool:
    return bool(HEADING.match(line.strip()))


def find_sections(markdown: str) -> list[Section]:
    """Recitals first, then articles.

    Two kinds because the document has two kinds of content: recitals explain
    the reasoning, articles state the binding rule. Keeping them apart in
    metadata is what lets retrieval filter to one or the other - and that
    filter was worth +36 points of recall@1 on this corpus.
    """
    first_article = HEADING.search(markdown)
    cut = first_article.start() if first_article else len(markdown)
    return _recitals(markdown[:cut]) + _articles(markdown)


def _recitals(region: str) -> list[Section]:
    """Numbering must run 1, 2, 3 ... without gaps.

    Anything out of sequence is a footnote marker or a cross-reference, not a
    recital. That sequence check is the whole defence against false positives,
    since '(1)' appears all over the document for other reasons.
    """
    kept, expected = [], 1
    for match in RECITAL.finditer(region):
        if int(match.group(1)) == expected:
            kept.append(match)
            expected += 1

    out = []
    for i, match in enumerate(kept):
        end = kept[i + 1].start() if i + 1 < len(kept) else len(region)
        out.append(
            Section(
                path=f"Recitals > Recital {i + 1}",
                body=region[match.end() : end].strip(),
                offset=match.end(),
                kind="recital",
                number=i + 1,
            )
        )
    return out


def _articles(markdown: str) -> list[Section]:
    """One section per article, carrying its chapter and section in the path."""
    matches = list(HEADING.finditer(markdown))
    out, chapter, section = [], "", ""

    for i, match in enumerate(matches):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()

        if label.startswith("CHAPTER"):
            chapter, section = label, ""
            continue
        if label.startswith("Section"):
            section = label
            continue

        # The article's title is the first non-empty line after the number. It
        # moves into the path, so drop it from the body rather than having
        # every chunk of the article repeat it twice.
        lines = body.splitlines()
        first = next((n for n, ln in enumerate(lines) if ln.strip()), None)
        title = lines[first].strip(" _*#") if first is not None else ""
        if first is not None:
            body = "\n".join(lines[first + 1 :]).strip()

        number = int(re.search(r"\d+", label).group())
        path = " > ".join(x for x in (chapter, section, f"{label}: {title}") if x)
        out.append(Section(path=path, body=body, offset=match.end(), kind="article", number=number))

    return out
