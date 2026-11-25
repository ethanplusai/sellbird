# WORKFLOW AGENTS

## OVERVIEW
Five async workflow agents; score 8, distinct orchestration domain with large sender, handler and scout modules.

## WHERE TO LOOK
| Task | Location | Contract |
|------|----------|----------|
| Find and score prospects | `scout.py` | Python searches/resolves; model scores collected inputs |
| Draft sequences | `writer.py` | Native email 1 per person; later steps campaign-level |
| Stage and drain | `sender.py` | Separate outbox creation from due approved sends |
| Read and classify replies | `handler.py` | Hard opt-out logic precedes model classification |
| Idle reporting | `analyst.py` | Atomic analytics JSON replacement |
| Draft shape | `../email_style.py`, `../../prompts/writer.md` | Deterministic validator plus model instructions |
| Untrusted material | `../untrusted.py`, `../site_facts.py` | Fenced data and evidence-qualified claims |

## CONVENTIONS
- Agent `run()` methods are called by the heartbeat, not independent background daemons.
- Native sending owns sequencing locally; Instantly deployment remains a separate legacy path.
- Render merge variables before staging; one native outbox item represents a prospect/step.
- Follow-up delays are relative to actual preceding sends, not original campaign creation.
- Preserve atomic claim semantics when draining; another Sender may claim the same due queue.
- Writer bounds unusable returned drafts to three attempts; provider outages do not consume attempts.
- Optional insufficient-grounding holds use `no_tip`; exhausted drafting uses `write_failed`.
- Custom operator rules replace built-in shape, not factual grounding or deterministic validation.
- Handler distinguishes recipient text from quoted history before applying opt-out rules.
- Real company replies stop colleagues' sequences; bounces and out-of-office replies differ.
- Native bounces invalidate the address, cancel queued mail and can trip a global kill switch.
- `conversation_history` retains recent messages and prior prospect constraints when trimming.
- Focused checks: `python -m pytest -q tests/test_outbox_native.py tests/test_company_stop.py tests/test_writer_retry.py`.

## ANTI-PATTERNS
- Do not let model output create, drop or merge Scout inputs while scoring them.
- Do not let a surname match alone establish that a person owns the company.
- Do not treat automated homepage absence as proof that online booking is unavailable.
- Do not retry mail whose provider outcome may already represent delivery.
- Do not release delayed follow-ups together or send a follow-up whose predecessor never sent.
- Do not respond automatically to legal/hostile escalation or reply to unsubscribe requests.
- Do not let quoted historical opt-out text masquerade as the recipient's current instruction.
