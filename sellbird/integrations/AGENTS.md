# EXTERNAL ADAPTERS

## OVERVIEW
Mailbox, verification, compliance and service adapters; score 8, distinct external-system boundary.

## WHERE TO LOOK
| Task | Location | Contract |
|------|----------|----------|
| Native mail interface | `mail_provider.py` | `MailProvider`, `SendResult`, `MailIdentity`, `TokenSource` |
| Gmail | `gmail.py` | REST OAuth, send, complete reads and history confirmation |
| Microsoft 365 | `microsoft.py` | Graph OAuth/PKCE, validated pagination |
| SMTP and IMAP | `smtp_mail.py` | Native SMTP sending and UNSEEN polling |
| Legacy sequencer | `instantly.py` | Separate campaign-owning API, not MailProvider |
| Address evidence | `email_finder.py` | Pattern-first resolution and one-candidate verification |
| Cold-mail clearance | `precleared.py` | Verdict fingerprint/cache, holds and outage scope |
| Warmup reads | `trulyinbox.py` | Read-only mailbox health and rejected-key latch |
| Message body/signature | `signature.py` | Gated text, escaped HTML and inline CID logo |
| Subscription utilization | `quota.py` | Best-effort gauge; local usage fallback |
| Existing HTML fingerprints | `tech_detect.py` | No extra HTTP requests |

## CONVENTIONS
- Gmail and Graph use HTTP APIs rather than vendor SDKs.
- Per-inbox `MailIdentity` supplies From identity; do not assume the main persona for every send.
- Inject `TokenSource` for extra inboxes; verify signed-in identity before saving OAuth credentials.
- `SendResult.uncertain` distinguishes possible delivery from definite refusal; it is not retryable failure.
- Graph final-send retry policy differs from safe preparatory requests.
- Failed/malformed/overflowing Gmail or Graph pagination invalidates the whole read.
- Read effects differ: Graph reads without marking, SMTP retrieves UNSEEN and marks seen.
- Published address evidence can improve cached defaults; another named colleague's address is excluded.
- Catch-all stays risky even if published; inconclusive verification stays guess.
- Count Hunter budget before requests; transient failure is not permanent absence.
- Precleared applies to native first cold contact, not replies; only `ok_to_email` permits sending.
- Precleared service outage holds differ from address-local failures and cached definite verdicts.
- A rejected TrulyInbox key is latched until reconnection; requests are GET-only.
- Configured but unusable signatures hold sending rather than silently dropping the logo.
- Focused checks: `python -m pytest -q tests/test_microsoft.py tests/test_gmail_inbox.py tests/test_email_evidence.py tests/test_precleared.py`.

## ANTI-PATTERNS
- Do not follow arbitrary Graph nextLink URLs with bearer credentials.
- Do not return partial inbox results as a complete successful read.
- Do not automatically retry an uncertain send.
- Do not expose verifier query URLs, access tokens or credentials in diagnostics.
- Do not add eager optional-heavy imports to this package initializer.
- Do not treat `calendar.py` as a working booking backend; its current methods are placeholders.
