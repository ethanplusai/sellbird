# Nixafin prospecting

Sellbird set up as a list-builder for Nixafin. It finds the decision-makers at
target companies and their email addresses, and exports them for the team to
email. It sends nothing: email is switched off in `sellbird.local.yaml`.

## Run it

```bash
nixafin/run.sh --import nixafin/companies.csv
```

1. Adds every company in `companies.csv` that isn't already known.
2. Reads each company's website: free, no model call.
3. Finds the people who decide and their email addresses, until every company
   has been checked. A company is checked again after 14 days.
4. Writes `nixafin/prospects-<date>.csv`.

`nixafin/run.sh --export-only` writes the CSV again without searching.

## Adding companies

Add rows to `companies.csv`: `name,website,type,country,source`. Only the
website is required. Good sources of small licensed money transfer operators,
remittance companies and bureaux de change are the regulators' public lists:
the Central Bank of Kenya's directory of money remittance providers, the
Central Bank of Nigeria's list of international money transfer operators, and
the Bank of Uganda's forex bureau and remittance lists.

## Letting it find companies itself

Add a search key to `.env`, then run with `--discover`:

```
TINYFISH_API_KEY=...     # free, no card: agent.tinyfish.ai/api-keys
```

```bash
nixafin/run.sh --discover
```

It searches each target type in each target country from `sellbird.local.yaml`.

## Email status in the CSV

| Status | Meaning |
|---|---|
| verified | A verifier confirmed the mailbox exists |
| risky | The domain accepts every address, so it can't be confirmed |
| guess | Built from the company's address pattern, not confirmed |

Everything is `guess` until a verifier key is in `.env`.
`REOON_API_KEY` gives 600 free checks a month (reoon.com).
Published general inboxes (`info@`, `sales@`) are real addresses the company
prints on its own site, even when marked `guess`.

## Files

| File | What it is |
|---|---|
| `sellbird.local.yaml` | Who Nixafin targets: company types, countries, titles |
| `skills/product_knowledge.md` | What Nixafin does, for Sellbird's scoring |
| `nixafin/companies.csv` | Companies to look into |
| `nixafin/prospects-<date>.csv` | Contacts found |
| `data/sellbird.db` | Everything Sellbird knows, in SQLite |

`nixafin/` is excluded from git (`.git/info/exclude`) so prospect data never
lands in the public Sellbird repo.
