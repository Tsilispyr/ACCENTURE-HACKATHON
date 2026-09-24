# The supplied knowledge pack lives with the code that reads it

The 11 PDFs are committed at:

    src/domains/vendor_risk/docs/
    src/domains/vendor_risk/docs/historical-vendor-assessments/

and the official handout at [../architecture/handout-official.pdf](../architecture/handout-official.pdf).

## Why not a second copy here

They were briefly committed here as well, byte-identical to the ones under
`docs/`. One copy was removed rather than kept, for a reason worth stating:

**`agentcore.rag.index` reads `docs/` and nothing else.** A second copy is not a
backup, it is a second thing to keep in step. Someone edits one, the index is
built from the other, and every downstream number is quietly measured against a
corpus nobody is looking at. That failure mode has already cost this project a
day once, when two stand-in markdown files were left beside a real corpus.

The "as supplied, untouched" copy that a second directory is meant to provide is
something git already has:

```bash
git log --oneline -- src/domains/vendor_risk/docs/    # every change to the pack
git show <commit>:src/domains/vendor_risk/docs/<file> # any earlier version
```

## Where the pack came from

The organisers' `knowledge-base/knowledge/` folder, unmodified. The layout under
`docs/` mirrors it, including the `historical-vendor-assessments/` subfolder,
which `corpus.py` picks up with `rglob` without anyone editing a path.

After changing anything in `docs/`, follow [../CORPUS.md](../CORPUS.md) in order.
Everything downstream of the corpus is derived from it and fails quietly.
