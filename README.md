# ACCENTURE-HACKATHON

Two hackathons, two self-contained folders. Nothing is shared between them: each has its own
handout, its own dependencies, its own containers and its own ports, so either can be cloned, read
and run without the other.

| | Folder | What it is |
|---|---|---|
| **Hackathon 1** | [HACKATHON_1/](HACKATHON_1/) | Incident response agent. Local environment, Langfuse infrastructure, Postgres, and the core agent application |
| **Hackathon 2** | [HACKATHON_2/](HACKATHON_2/) | **AI-Powered Vendor Risk & Procurement Deep Agent.** RAG over the Northstar knowledge pack, guardrails, MCP tools, specialist reviewers, evaluation |

Start with the README inside whichever folder you want. Each is the entry point for that project
and carries the handout it was built against.

## Why they share nothing

They cannot run at the same time on one machine. WSL 2 has about 3.6 GB here and the hackathon 1
stack alone uses roughly 2.4 GB of it, so hackathon 2's `scripts/deploy.sh` checks for hackathon 1's
containers and refuses to start rather than letting both die of OOM halfway through. Container
names, ports and compose project names are deliberately distinct on both sides for the same reason.

Sharing code would also mean either project could break the other, which is the opposite of what a
folder per hackathon is for.

## Layout

```
HACKATHON_1/     incident response agent: source, tests, handout, task board
HACKATHON_2/     vendor risk deep agent: source, tests, handout, knowledge pack, evaluation results
```

Only `.gitignore` and `.gitattributes` sit above those two folders, because only they apply to both.
