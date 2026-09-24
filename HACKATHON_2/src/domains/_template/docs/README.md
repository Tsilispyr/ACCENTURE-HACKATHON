# Put the corpus here

Drop the scenario's documents into this directory. PDF, `.md` and `.txt` all
work; PDFs are parsed to markdown and cached on a hash of the file.

Then:

    DOMAIN=<name> uv run python -m agentcore.rag.index --reset

Delete this file once you have added real documents - otherwise it is indexed
along with them, and "put the corpus here" becomes a retrievable answer.
