# Master AI Privacy Policy

Last updated: 2026-04-25

Master AI is local-first software. Its default behavior is to run on the buyer's own machine and store working data on that machine.

## What Stays Local

- Chat history and saved sessions
- Project boards and task notes
- Memory files
- Approved command lists
- Router feedback metrics
- Local model prompts and responses handled by Ollama

These files live under the buyer's home directory, usually in `~/scripts`, `~/.master_ai_*`, and `~/.master_ai_chats`.

## Optional Cloud Use

Cloud calls happen only when the buyer configures API keys or explicitly chooses a cloud route such as `fast:`, `deep:`, or Connected Mode. When cloud fallback is used, Master AI prints a visible warning.

Depending on the buyer's configured keys, prompts may be sent to providers such as Groq, OpenRouter, Gemini, OpenAI, Anthropic, or DeepSeek. Those providers' own privacy policies apply to their services.

## What Master AI Does Not Do

- It does not sell personal data.
- It does not include third-party telemetry.
- It does not ask for passwords.
- It does not run `sudo` commands inside Sensei.
- It does not upload local files unless the buyer explicitly asks for a workflow that sends data to an external service.

## Logs

Master AI writes local diagnostic logs so the buyer can troubleshoot their own installation. Logs are stored locally and can be deleted by the buyer.

## Support Data

If a buyer asks for help, they may choose to share logs, screenshots, or command output. They should remove secrets before sharing. Master AI never requires API keys, passwords, private documents, or payment information for support.
