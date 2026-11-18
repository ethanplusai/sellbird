"""Nixafin prospecting runner: import companies, find their contacts, export.

Runs Sellbird's own stages back to back instead of its 15-minute loop:

  1. import   (optional) add companies from a CSV of websites
  2. discover (optional) find companies by web search, with a search key
  3. profile  read each company's website (free, no model call)
  4. contacts find the decision-makers and their email addresses
              (Sellbird's Scout; repeats until every company has been checked)
  5. export   write the contacts to a CSV for the team

Sends nothing: email sending is off in sellbird.local.yaml.

    python nixafin/prospect.py --import nixafin/companies.csv
    python nixafin/prospect.py --discover        # needs TINYFISH_API_KEY in .env
    python nixafin/prospect.py --export-only
"""

import argparse
import asyncio
import csv
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from sellbird.config import load_config, load_env      # noqa: E402
from sellbird.models.company import Company             # noqa: E402
from sellbird.state import StateManager                 # noqa: E402

log = logging.getLogger("nixafin")
MAX_CONTACT_ROUNDS = 30          # 10 companies a round, so up to 300 companies


def domain_of(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    host = urlparse(value if "://" in value else f"https://{value}").netloc
    return host.split(":")[0].removeprefix("www.")


async def import_companies(state: StateManager, path: Path) -> None:
    added = known = skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            domain = domain_of(row.get("website") or row.get("domain", ""))
            if not domain:
                skipped += 1
                continue
            if await state.get_company_by_domain(domain):
                known += 1
                continue
            await state.add_company(Company(
                name=row.get("name") or domain,
                domain=domain,
                website=f"https://{domain}",
                industry=row.get("type") or row.get("industry", ""),
                location=row.get("country") or row.get("location", ""),
                source="import",
                source_url=row.get("source", ""),
            ))
            added += 1
    print(f"Import: {added} added, {known} already known, {skipped} rows without a website.")


async def find_contacts(config, env, state: StateManager) -> None:
    from sellbird.agents.scout import Scout
    from sellbird.brain import Brain
    from sellbird.pipeline import run_profile_stage

    brain = Brain(state, config.brain)
    problem = await brain.preflight()
    if problem:
        sys.exit(f"Claude isn't usable right now: {problem}")
    scout = Scout(brain, state, config, env)

    for round_no in range(1, MAX_CONTACT_ROUNDS + 1):
        await run_profile_stage(state, limit=200, config=config)
        waiting = await state.companies_needing_prospects(limit=1000)
        if not waiting:
            break
        print(f"Contacts, round {round_no}: {len(waiting)} companies still to check...")
        await scout.run()
    else:
        print(f"Stopped after {MAX_CONTACT_ROUNDS} rounds; run again to continue.")


def discover() -> None:
    if not (os.environ.get("TINYFISH_API_KEY") or os.environ.get("SERPER_API_KEY")):
        sys.exit("Discovery needs a search key: TINYFISH_API_KEY (free) or "
                 "SERPER_API_KEY in .env. Without one, use --import.")
    provider = "tinyfish" if os.environ.get("TINYFISH_API_KEY") else "serper"
    subprocess.run([sys.executable, "-m", "sellbird", "discover", "--provider", provider,
                    "--no-profile"], check=False)


def export(out: Path) -> None:
    # Unverified guesses included (no verifier key yet); opted-out contacts stay out.
    subprocess.run([sys.executable, "-m", "sellbird", "export", "--out", str(out),
                    "--email-status", "verified,risky,guess"], check=False)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import", dest="import_csv", type=Path,
                        help="CSV with columns name, website, type, country")
    parser.add_argument("--discover", action="store_true",
                        help="also search for companies (needs a search key)")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "nixafin" / f"prospects-{date.today()}.csv")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpcore", "sellbird.brain"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = load_config()
    env = load_env()
    if config.channels.email.enabled:
        sys.exit("Email sending is on in the config; this runner is for list-building only.")
    state = StateManager()
    await state.init_db()

    if not args.export_only:
        if args.import_csv:
            await import_companies(state, args.import_csv)
        if args.discover:
            discover()
        await find_contacts(config, env, state)
    export(args.out)


if __name__ == "__main__":
    asyncio.run(main())
