# HRmagistr

A Telegram job-market assistant that runs unattended on a laptop. A read-only userbot collects job posts from Telegram channels and chats, an LLM turns each post into a structured card and scores it against your profile, rule-based checks catch what the model gets wrong, and a dashboard, a bot and an MCP server keep you on top of the market.

Full documentation in Russian: [docs/README.ru.md](docs/README.ru.md).

## What it does

- **Collects** posts from a Telegram folder you control (drop a channel into the folder, it gets picked up in about 2 minutes), from public channels it reads without joining, and from sources it discovers on its own by search. Dead sources are pruned automatically.
- **Understands** each post with an LLM: title, salary range, format, stack, vertical, contacts, job vs. one-off project, and a 0-100 relevance score against `profile.md`.
- **Does not trust the model blindly:**
  - contacts are kept only if they literally appear in the post (including bare usernames like `Contacts: name`);
  - a technical category from the model is accepted only if the post itself contains technical vocabulary, so a call-centre ad cannot become "Head of Tech";
  - candidate resumes are recognised by rules and never reach the vacancy pipeline;
  - cross-posted duplicates are merged and a keyword prefilter drops noise before any LLM call.
- **Flags risk and context on every card:** scam patterns (run our repo, connect a wallet, pay for training), employment terms (labour contract, contractor, B2B, "Russia only"), and recruiters you have already talked to.
- **Tracks two searches at once:** the main one (relevance score) and a rule-based backup track, with no extra LLM calls.
- **Collects the other side of the market:** candidate resumes are parsed by rules and enriched by a local model in the background, so you can compare what employers ask for with what candidates offer.
- **Helps with the next step:** reply drafts in your own voice, the CV you sent attached to each vacancy, and interview prep generated when a card moves to "Interview". Nothing is ever sent automatically.
- **Exposes itself to an AI assistant** through an MCP server: list and read vacancies, save drafts and prep notes, set statuses and contacts, read and update the application tracker, open a chat with a recruiter. Sending messages stays with a human.

## How it works

```
Telegram folder ─┐
Public channels ─┼─► collector (history backfill + live updates + polling)
Auto-discovery ──┘                │
                                  ▼
   resume detector ─► dedup ─► keyword prefilter ─► LLM extraction ─► rule checks
        │                                                                  │
        ▼                                                                  ▼
   resumes (local LLM, background)        dashboard (FastAPI + HTMX + Chart.js)
                                          Telegram bot: matches, digest, alerts
                                          MCP server for an AI assistant
```

- **LLM chain:** Gemini first, then Groq, then a local model through Ollama. Each provider has its own rate-limit handling and a daily quota cap. Failed items are retried later automatically.
- **Priority queue for the local model:** vacancies always go first; background work (resume enrichment) runs only when the vacancy queue is empty and yields between items.
- **Network:** all external traffic can go through one SOCKS5/HTTP proxy with a kill switch. If the proxy is down, the app refuses to start instead of falling back to a direct connection.
- **Unattended:** a watchdog restarts the app with backoff, a Windows scheduled task starts it at logon, the system is kept awake while on AC power, and missed posts are caught up after sleep.
- **Read-only by design:** the collector never sends messages or joins chats.

## Stack

Python 3.12, Telethon (MTProto), FastAPI, Jinja2 + HTMX, Chart.js, SQLite (WAL), httpx, MCP (Python SDK), pypdf, Gemini / Groq / Ollama through OpenAI-compatible APIs.

## Quick start

```powershell
uv venv .venv --python 3.12
uv pip install -p .venv\Scripts\python.exe -r requirements.txt
copy .env.example .env          # Telegram api_id/api_hash, LLM key, optional bot and proxy
copy profile.example.md profile.md
.venv\Scripts\python run.py     # first run asks for the phone number and login code
```

Dashboard: http://127.0.0.1:8765

Run in the background with the watchdog: `.venv\Scripts\pythonw run_forever.py`, or install autostart with `tools\install_autostart.ps1`. Status and stop: `tools\status.ps1`, `tools\stop.ps1`.

## LLM setup

The app works with any OpenAI-compatible endpoint. The default chain in `config.yaml` is Gemini, then Groq, then a local model; you need at least one of them.

1. **Gemini (recommended, free tier).** Get a key at [Google AI Studio](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY` in `.env`. The free tier gives roughly 450 requests a day; the app stops at `daily_limit` (430 by default) and falls back to the next provider.
2. **Groq (optional fallback).** Key from [console.groq.com](https://console.groq.com/keys) into `GROQ_API_KEY`. If Groq is blocked in your region, set `GROQ_PROXY`.
3. **Local model through Ollama (optional, private, no quotas).**
   - Install Ollama from [ollama.com](https://ollama.com/download) and make sure it runs (`ollama list`).
   - Pull a model. The default is `qwen3-vl:8b-instruct`; any instruction-tuned model with JSON output works:
     ```powershell
     ollama pull qwen3-vl:8b-instruct
     ```
   - Keep `OLLAMA_BASE_URL=http://localhost:11434/v1` in `.env`. To use another model, change `model:` of the `ollama` entry in `config.yaml`.
   - Rough sizing: an 8B model at Q4 needs about 6 GB of VRAM to run fully on the GPU; 14B about 10 GB; 32B about 20 GB. With less VRAM Ollama offloads to CPU and gets slower, but still works.
   - Background resume enrichment uses only the local model and only when the vacancy queue is empty; turn it off with `market.resume_llm: false`.

MCP server for an AI assistant (any MCP client):

```json
{"mcpServers": {"hrmagistr": {"command": ".venv\\Scripts\\python.exe", "args": ["mcp_server.py"]}}}
```

## Configuration

- `.env`: secrets (Telegram API, LLM keys, bot token, proxy, dashboard password). Never committed.
- `profile.md`: who you are and what you are looking for. Drives relevance and reply drafts. Never committed.
- `config.yaml`: sources, prefilter and tech-evidence vocabularies, resume markers, LLM chain and quotas, backup track rules, autopilot, market settings.

## Use a separate Telegram account

Run the collector on a dedicated account, not your personal one. It only reads, but a dedicated account keeps your main one out of any risk.
