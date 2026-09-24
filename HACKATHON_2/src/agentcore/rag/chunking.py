"""Corpus -> structure-aware chunks.

Ported from the course project's 61_pdf_pipeline.py, with two changes:
chunk size is a parameter rather than a module global that callers mutate, and
section detection is supplied by the domain rather than hard-coded to one
document's vocabulary.

The order matters and each step exists for a measured reason:

    load -> strip_boilerplate -> normalize -> join -> sections -> split

  strip_boilerplate  a running footer appears on every page; in the reference
                     corpus it was spliced MID-SENTENCE on all 88 pages, so
                     leaving it in corrupts 88 sentences and puts 88 copies of
                     junk in the index.
  normalize          soft hyphens, footnote markers and stray tags survive any
                     output format - they are already in the extracted text.
                     'pseudony<shy> misation' indexes as two words that match
                     nothing.
  join               concatenate pages BEFORE splitting so a sentence spanning
                     a page break stays whole; an offset map preserves the page
                     number for citations.
  sections           split on document structure FIRST, size second, so no
                     chunk straddles two unrelated sections.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Callable, NamedTuple

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# '(<sup>1</sup> )' is a footnote marker sitting mid-sentence.
FOOTNOTE_REF = re.compile(r"\s*\(\s*<sup>\d+</sup>\s*\)")
INLINE_TAG = re.compile(r"</?(?:u|sup|sub|b|i)>")

# The generic fallback: real markdown headings.
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

# pymupdf4llm renders a bold PDF heading as '## **1. Scope**'. Only WRAPPING
# markers are removed, so an underscore inside a word survives.
EMPHASIS = re.compile(r"(\*\*|__|\*|_)(.+?)\1")


class Section(NamedTuple):
    """One structural unit of the document."""

    path: str  # "CHAPTER IV > Article 33: Notification..."
    body: str
    offset: int  # character offset into the joined text, for page lookup
    kind: str = "section"  # domains use this for metadata filtering
    number: int = 0  # 33 for Article 33 - enables locator lookup


SectionFinder = Callable[[str], list[Section]]


# ---------------------------------------------------------------- loading --


def load_pages(paths: list[Path], cache_dir: Path | None = None) -> list[dict]:
    """One {page, text} dict per source page.

    PDFs go through pymupdf4llm (markdown out, page-chunked). Text and markdown
    files are read as a single page. Parsing is slow and its output needs
    eyeballing, so results are cached on a hash of the file.
    """
    pages: list[dict] = []
    for path in paths:
        if path.suffix.lower() == ".pdf":
            pages.extend(_load_pdf(path, cache_dir))
        else:
            pages.append({"page": 1, "text": path.read_text(encoding="utf-8"), "source": path.name})
    return pages


def _load_pdf(pdf: Path, cache_dir: Path | None) -> list[dict]:
    import pymupdf4llm

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
    cached = (cache_dir / f"{pdf.stem}-{digest}.json") if cache_dir else None

    if cached and cached.exists():
        print(f"  cache hit: {cached.name}")
        return json.loads(cached.read_text(encoding="utf-8"))

    print(f"  parsing {pdf.name} (no cache; this takes a minute)...")
    parsed = pymupdf4llm.to_markdown(str(pdf), page_chunks=True, show_progress=False)
    pages = [
        {"page": p["metadata"]["page_number"], "text": p["text"], "source": pdf.name}
        for p in parsed
        if p["text"].strip()
    ]
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(pages), encoding="utf-8")
    return pages


# ------------------------------------------------------------- cleaning ----


def as_template(line: str) -> str:
    """Blank digits so 'L 119/1' and 'L 119/2' count as the same line."""
    return re.sub(r"\d+", "#", line.strip())


def strip_boilerplate(pages: list[dict], is_heading: Callable[[str], bool]) -> list[dict]:
    """Drop lines repeating across most pages - running headers and footers.

    Two passes. Page edges catch true headers/footers; a second any-position
    pass catches furniture that floats mid-page. Both must be digit-blind,
    because the footer carries the page number.

    The heading guard is not optional: without it, digit-blind matching sees
    'Article 1', 'Article 2', ... as ONE repeated line and deletes every
    heading in the document - silently, leaving a handful of huge sections.
    """
    if len(pages) < 3:
        return pages

    headings_before = sum(1 for p in pages for ln in p["text"].splitlines() if is_heading(ln))

    edge, counts, anywhere = 3, Counter(), Counter()
    for page in pages:
        lines = [ln for ln in page["text"].splitlines() if ln.strip()]
        for line in lines[:edge] + lines[-edge:]:
            if not is_heading(line):
                counts[as_template(line)] += 1
        for line in lines:
            if not is_heading(line):
                anywhere[as_template(line)] += 1

    threshold = max(3, len(pages) // 4)
    boilerplate = {t for t, n in counts.items() if n >= threshold and len(t) < 90}
    boilerplate |= {t for t, n in anywhere.items() if n >= threshold}

    if boilerplate:
        print(f"  dropping {len(boilerplate)} boilerplate pattern(s):")
        for t in sorted(boilerplate, key=lambda t: -max(counts[t], anywhere[t]))[:6]:
            print(f"     {max(counts[t], anywhere[t]):>3}x  {t[:66]!r}")

    for page in pages:
        page["text"] = "\n".join(
            line
            for line in page["text"].splitlines()
            if is_heading(line) or as_template(line) not in boilerplate
        )

    # Deleting a heading here collapses the document silently. Fail loudly.
    headings_after = sum(1 for p in pages for ln in p["text"].splitlines() if is_heading(ln))
    if headings_after < headings_before:
        raise RuntimeError(
            f"boilerplate removal deleted {headings_before - headings_after} heading(s); "
            "a repeated-line rule is matching document structure"
        )
    return pages


def normalize(pages: list[dict]) -> list[dict]:
    """Repair typesetting artifacts. No output format fixes these."""
    for page in pages:
        # Both dashes below are ESCAPES, not literal characters, and both are
        # load-bearing. A literal invisible character in source is a trap: it
        # survives no diff, no review and no reformat, and deleting it breaks
        # retrieval silently.
        #
        # U+00AD is a soft hyphen. A PDF puts one at a line break, so
        # "sub\u00ad\nprocessor" is one word that matches neither
        # "subprocessor" nor "sub-processor" until the pair is removed.
        text = re.sub("\u00ad\\s*", "", page["text"])
        # Written as an escape, not the literal character. It is the one
        # dash in this codebase that MUST survive: PDFs use a non-breaking
        # hyphen (U+2011) that never matches a plain "-", so a heading like
        # "Article 33" silently stops matching if this normalisation goes.
        text = text.replace("\u2011", "-")
        text = FOOTNOTE_REF.sub("", text)
        text = INLINE_TAG.sub("", text)
        page["text"] = "\n".join(line.rstrip() for line in text.splitlines())
    return pages


def reflow(pages: list[dict], is_heading: Callable[[str], bool]) -> list[dict]:
    """Rejoin lines the PDF's typesetting broke mid-sentence.

    Only needed for plain-text extraction. RecursiveCharacterTextSplitter
    prefers '\\n' as a boundary, so hard-wrapped text makes it split where the
    typesetter ran out of column width rather than where the meaning ends.
    """
    new_block = re.compile(r"^\s*(\(\w{1,3}\)|\d{1,2}\.)\s")
    for page in pages:
        out: list[str] = []
        for line in page["text"].splitlines():
            stripped = line.strip()
            if not stripped:
                out.append("")
                continue
            starts = (
                not out
                or out[-1] == ""
                or is_heading(stripped)
                or is_heading(out[-1])
                or new_block.match(stripped)
                or out[-1].rstrip().endswith((".", ";", ":"))
            )
            out.append(stripped) if starts else out.__setitem__(-1, f"{out[-1]} {stripped}")
        page["text"] = "\n".join(out)
    return pages


# ------------------------------------------------------------- assembly ----


def join(pages: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Concatenate pages, remembering where each starts.

    Joining first keeps a page-straddling sentence whole; the offset map is
    what still lets a chunk cite its page.
    """
    parts, offsets, cursor = [], [], 0
    for page in pages:
        offsets.append((cursor, page["page"]))
        parts.append(page["text"])
        cursor += len(page["text"]) + 1
    return "\n".join(parts), offsets


def page_at(offset: int, offsets: list[tuple[int, int]]) -> int:
    page = offsets[0][1] if offsets else 1
    for start, number in offsets:
        if start > offset:
            break
        page = number
    return page


def markdown_sections(markdown: str) -> list[Section]:
    """The generic default: split on markdown headings, tracking the hierarchy.

    Domains whose corpus has real structure (articles, clauses, SOP steps)
    override this with something that understands it - see
    domains/sample_policy/corpus.py.
    """
    matches = list(MARKDOWN_HEADING.finditer(markdown))
    if not matches:
        return [Section(path="document", body=markdown.strip(), offset=0)]

    out, stack = [], []
    for i, match in enumerate(matches):
        # Without this the label reads '**Policy** > **1. Scope**', and that
        # is what every citation built from it would show.
        level, title = len(match.group(1)), EMPHASIS.sub(r"\2", match.group(2)).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        stack = stack[: level - 1] + [title]
        body = markdown[match.end() : end].strip()
        if body:
            # Sections are NUMBERED even in the generic path. Without a number
            # there is no stable label for an eval case to name and no locator
            # for a reader to ask for - "section 3" has to mean something.
            out.append(
                Section(
                    path=" > ".join(stack),
                    body=body,
                    offset=match.end(),
                    kind="section",
                    number=len(out) + 1,
                )
            )
    return out


def build_chunks(
    markdown: str,
    offsets: list[tuple[int, int]],
    sections: list[Section],
    *,
    source: str,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
    start_index: int = 0,
) -> list[Document]:
    """Split WITHIN each section, then prepend the section path to each chunk.

    The prefix is the cheap, deterministic half of the context problem. On its
    own a chunk reading 'the controller shall implement appropriate measures'
    is generic boilerplate that matches nothing; with its path it is findable.

    `start_index` continues the reading-order counter across files.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
    )
    chunks: list[Document] = []

    for section in sections:
        for piece in splitter.split_documents([Document(page_content=section.body)]):
            absolute = section.offset + piece.metadata.get("start_index", 0)
            chunks.append(
                Document(
                    page_content=f"{section.path}\n\n{piece.page_content}",
                    metadata={
                        "source": source,
                        "page": page_at(absolute, offsets),
                        "section": section.path,
                        "kind": section.kind,
                        "number": section.number,
                        # Document order. Retrieval returns rows by distance, so
                        # this is the only way to restore reading order.
                        "index": start_index + len(chunks),
                    },
                )
            )
    return chunks


def chunk_corpus(
    paths: list[Path],
    *,
    source: str,
    section_finder: SectionFinder = markdown_sections,
    is_heading: Callable[[str], bool] | None = None,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
    cache_dir: Path | None = None,
    do_reflow: bool = False,
) -> list[Document]:
    """The whole pipeline, end to end - run ONCE PER FILE.

    Per file because a citation has to name a document. Joining every file
    into one stream labelled every chunk with `source` (the domain name), and
    page numbers restarted in each file, so "page 3" could mean eleven places.
    Boilerplate is per file too: one PDF's running footer is not another's.

    POSITIONAL section numbers (kind "section", what markdown_sections emits)
    continue across files in path order, so "section 9" is still one place for
    eval labels and locator lookups. Semantic numbers from a domain's own finder
    ("article 33") belong to the document and are left alone.
    `source` is only the fallback label for a page that carries none.
    """
    if is_heading is None:
        is_heading = lambda line: bool(MARKDOWN_HEADING.match(line.strip()))  # noqa: E731

    pages = load_pages(paths, cache_dir)
    if not pages:
        raise RuntimeError(f"No readable pages in: {[str(p) for p in paths]}")

    # Corpus-wide first, then per file below. A pack of one-page PDFs has a
    # footer on every FILE but never three pages in one, so the per-file pass
    # alone cannot see it. For a single-file corpus the two passes see the
    # same pages.
    pages = strip_boilerplate(pages, is_heading)

    by_file: dict[str, list[dict]] = {}
    for page in pages:
        by_file.setdefault(page.get("source") or source, []).append(page)

    chunks: list[Document] = []
    numbered = 0
    for name, file_pages in by_file.items():
        file_pages = strip_boilerplate(file_pages, is_heading)
        if do_reflow:
            file_pages = reflow(file_pages, is_heading)
        file_pages = normalize(file_pages)

        markdown, offsets = join(file_pages)
        sections = [
            s._replace(number=s.number + numbered) if s.kind == "section" and s.number else s
            for s in section_finder(markdown)
        ]
        numbered = max([numbered] + [s.number for s in sections if s.kind == "section"])

        chunks += build_chunks(
            markdown,
            offsets,
            sections,
            source=name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            start_index=len(chunks),
        )
        print(f"  {name}: {len(file_pages)} pages -> {len(markdown):,} chars -> {len(sections)} sections")
    print(f"  {len(by_file)} file(s) -> {len(chunks)} chunks")
    return chunks
