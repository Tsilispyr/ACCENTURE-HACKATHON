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

They share no code, no dependencies and no running services. Container names, ports and compose
project names are distinct on both sides, so neither project can break or collide with the other.
Each folder's `scripts/deploy.sh` explains the operational constraints that shaped its own setup.

## Layout

```
HACKATHON_1/     incident response agent: source, tests, handout, task board
HACKATHON_2/     vendor risk deep agent: source, tests, handout, knowledge pack, evaluation results
```

Only `.gitignore` and `.gitattributes` sit above those two folders, because only they apply to both.
