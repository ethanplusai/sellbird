"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sellbird.brain import Brain
from sellbird.config import EmailRulesError, SellbirdConfig, compliance_of, load_email_rules
from sellbird.email_style import (
    BREAKUP_MAX_WORDS, FIRST_EMAIL_MAX_WORDS, FOLLOW_UP_MAX_WORDS,
    style_problems, tidy, with_sign_off,
)
from sellbird.humanize import humanize, humanizer_enabled
from sellbird.integrations.mail_provider import NATIVE_PROVIDERS
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.state import WRITABLE_EMAIL_STATUSES, StateManager
from sellbird.sending_mode import sync_sending_mode
from sellbird import research as company_research
from sellbird.site_facts import site_facts
from sellbird.startups import startup_facts
from sellbird.untrusted import UNTRUSTED_RULE, fence

# A prospect with nothing to build a tip on (writer.require_tip) is parked here.
NO_TIP_STATUS = "no_tip"
# Research judged the company a competitor or a poor fit (writer.research).
UNFIT_STATUS = "unfit"
# The researched draft failed its self-check twice (writer.self_check).
CHECK_FAILED_STATUS = "check_failed"

logger = logging.getLogger("sellbird.writer")

# How many times the model may fail to write a usable sequence for a
# prospect before Sellbird stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Which signal_playbook sections a prospect's facts call for, by heading
# prefix and the words in the facts that point to them.
_SIGNAL_SECTIONS = (
    ("### Hiring signal", ("hiring", "job post", "is hiring", "open role", "careers")),
    ("#### Using a job posting", ("hiring", "job post", "open role")),
    ("### Tech-stack signal", ("tools detected", "uses ", "built on", "shopify", "hubspot", "wordpress")),
    ("### Funding signal", ("raised", "funding", "seed", "series a", "series b", "y combinator", "accelerator")),
    ("### New-domain", ("new domain", "registered", "just launched", "new business")),
    ("### Search-rank signal", ("searched", "search", "came up", "ranks", "google", "maps", "local pack")),
)


def _sections(markdown: str) -> dict[str, str]:
    """Each heading line of ``markdown`` mapped to the text under it."""
    out, head, lines = {}, "", []
    for line in markdown.split("\n"):
        if line.startswith("#"):
            if head:
                out[head] = "\n".join(lines).strip()
            head, lines = line.strip(), []
        else:
            lines.append(line)
    if head:
        out[head] = "\n".join(lines).strip()
    return out


# Any merge variable, for checking a rewrite kept the draft's own.
MERGE_VAR = re.compile(r"\{\{\s*\w+\s*\}\}")


def _names(prospect) -> list[str]:
    """Text that belongs to the prospect (their name, their company), so the
    style rules never read "Elevate Fitness" as an AI tell."""
    return [n for n in (getattr(prospect, "company", ""), getattr(prospect, "first_name", ""),
                        getattr(prospect, "last_name", ""), getattr(prospect, "title", "")) if n]


# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50

# The built-in "how" lines each prompt ends with: email 1 is an observation
# and a question, with no pitch. Operator email rules replace exactly these.
PERSONAL_SHAPE = f"""- 60 words at most, and aim for 30-50. Every word you cut raises the reply rate.
- One specific finding from the FACTS, what it likely costs them in one plain
  sentence, then YOUR OFFER below as the one question. Don't name the product.
- Write the actual text (no merge variables: you know their name/company).
- No sign-off and no name at the end: Sellbird adds the sender's first name.
- Subject: lowercase, 2-5 words."""
SEQUENCE_SHAPE = f"""- Email 1: One specific finding about them, what it costs them, then YOUR OFFER below as
  the one question. {FIRST_EMAIL_MAX_WORDS} words at most, aim for 30-50. Don't name the product.
- Email 2: 3 days later. A different angle: one concrete proof point or result from the
  product info, then the offer again or a short call as the ask. {FOLLOW_UP_MAX_WORDS} words at most, aim for 20-40.
- Email 3: 4 days after that. Close the loop in one or two plain sentences, and leave
  the offer open. {BREAKUP_MAX_WORDS} words at most, aim for 10-20. No guilt.
- No sign-off and no name at the end of any email: Sellbird adds the sender's first name."""
SEQUENCE_STYLE = """- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words."""
# Why the Writer stopped: Today and `sellbird status` show it.
EMAIL_RULES_PROBLEM_KEY = "email_rules_problem"
BUILT_IN_FOOTER = "- No postal address, unsubscribe or opt-out line: Sellbird adds them when it sends."


def operator_rules_block(rules: str) -> str:
    """The operator's email rules as a prompt section ("" when there are none).

    Trusted text from the person running Sellbird, so it isn't fenced. It wins
    over the built-in style and shape rules, never over the FACTS rule."""
    if not rules:
        return ""
    return (
        "\n\nOPERATOR'S EMAIL RULES, written by the person running Sellbird. For\n"
        "length, structure, what to say about the product, links, subject lines\n"
        "and sign-off, these replace the STRICT EMAIL RULES and skills above\n"
        "wherever the two disagree. They never override the FACTS rule: what you\n"
        "say about the prospect comes only from the facts given, never invented.\n"
        "Nor do they allow a fake \"Re:\" or \"Fwd:\" subject line, or a claim of a\n"
        "conversation that never happened.\n"
        + rules
    )


def offer_block(config) -> str:
    """What email 1 offers: ``product.offer.entry``, the free, useful thing you
    can really deliver (an audit, a mockup, a sample report).

    Without one, email 1 can only ask a question, and a question alone gives
    the reader nothing; so the writer falls back to that and says so."""
    offer = getattr(getattr(config, "product", None), "offer", None)
    entry = str(getattr(offer, "entry", "") or "").strip()
    if not entry:
        return ("\n\nYOUR OFFER: none set (product.offer.entry). End email 1 with one easy\n"
                "question about the problem you found instead.")
    return (f"\n\nYOUR OFFER for a first email: {entry}\n"
            "Something free and useful you can really deliver. Email 1 offers it as the one\n"
            "question, tied to what you found about them, in a few plain words. Offer only\n"
            "this, as it's written here: don't say you already made or did something unless\n"
            "it says so, and never promise anything else.")


def footer_rule(config, rules: str) -> str:
    """What the prompt says about the lines Sellbird adds when it sends.

    With operator rules it names only what Sellbird really adds: an opt-out
    sentence the operator writes into the body must not sit on top of the
    configured opt_out_line, and must not be dropped when there is none."""
    if not rules:
        return BUILT_IN_FOOTER
    compliance = compliance_of(config)
    lines = []
    if compliance.postal_address.strip():
        lines.append("- No postal address: Sellbird adds it under the email when it sends.")
    opt_out = compliance.opt_out_line.strip()
    if opt_out:
        lines.append(
            f'- Sellbird adds this opt-out line under the email when it sends: "{opt_out}".\n'
            "  Don't write another way to opt out, even if the operator's rules give one.")
    else:
        lines.append(
            "- Sellbird adds no opt-out line. If the operator's rules give a sentence that\n"
            "  lets the reader say no, write it into the body as they say.")
    return "\n".join(lines)


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: SellbirdConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")
        # The offer can change in the dashboard while Sellbird runs: use the
        # one saved now, not the one read at startup. A hosted workspace's
        # config comes from its own store, never a file, so it's left alone.
        from sellbird.workspace_home import current_home
        if not current_home().hosted:
            try:
                from sellbird.config import load_config
                self.config.product.offer = load_config().product.offer
            except Exception:
                pass
        try:
            self.email_rules = load_email_rules(self.config)
        except EmailRulesError as e:
            # The operator replaced the built-in shape on purpose: drafting in
            # it anyway would put emails they didn't want in the outbox.
            logger.warning(
                f"Writer: your email rules (writer.email_rules) can't be used: {e}. "
                "Writing nothing until that's fixed.")
            await self.state.set_setting(EMAIL_RULES_PROBLEM_KEY, str(e))
            return
        if await self.state.get_setting(EMAIL_RULES_PROBLEM_KEY):
            await self.state.delete_setting(EMAIL_RULES_PROBLEM_KEY)

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # channels.email.min_write_score: a prospect scored below it stays
        # 'new' and is never written for. 0 (the default) keeps everyone.
        min_score = self._min_write_score()
        if min_score > 0:
            kept = [p for p in prospects_with_email if (p.score or 0) >= min_score]
            skipped = len(prospects_with_email) - len(kept)
            if skipped:
                logger.info(
                    f"Writer: Skipping {skipped} prospect(s) scored below "
                    f"channels.email.min_write_score ({min_score})."
                )
            prospects_with_email = kept
            if not prospects_with_email:
                logger.info("Writer: No prospects at or above the minimum score.")
                return

        # writer.research: read each company's site, drop competitors and poor
        # fits before any draft is written for them.
        self._research: dict[str, dict | None] = {}
        if self._research_on():
            prospects_with_email = await self._research_gate(prospects_with_email)
            if not prospects_with_email:
                return

        # writer.require_tip: no draft for a prospect with nothing concrete to
        # build its tip on. Parked as "no_tip" rather than left "new", which
        # would make the heartbeat try to write for it every cycle.
        if self._require_tip():
            kept = []
            for p in prospects_with_email:
                if await self._has_tip_basis(p):
                    kept.append(p)
                else:
                    await self.state.update_prospect_status(p.id, NO_TIP_STATUS)
            parked = len(prospects_with_email) - len(kept)
            if parked:
                logger.info(
                    f"Writer: Not writing for {parked} prospect(s) with nothing concrete "
                    f"to build a tip on (writer.require_tip); marked {NO_TIP_STATUS}.")
            prospects_with_email = kept
            if not prospects_with_email:
                return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            self._no_reply = False
            sequence = await self._write_sequence(prospects)
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects, sequence)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    def _min_write_score(self) -> int:
        """channels.email.min_write_score, or 0 when the config has none."""
        email = getattr(getattr(self.config, "channels", None), "email", None)
        try:
            return max(0, int(getattr(email, "min_write_score", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _require_tip(self) -> bool:
        """writer.require_tip, or False when the config has none."""
        return bool(getattr(getattr(self.config, "writer", None), "require_tip", False))

    def _research_on(self) -> bool:
        return bool(getattr(getattr(self.config, "writer", None), "research", False))

    def _self_check_on(self) -> bool:
        return bool(getattr(getattr(self.config, "writer", None), "self_check", False))

    def _profile(self, prospect) -> dict | None:
        return getattr(self, "_research", {}).get(prospect.company_id or "")

    async def _research_gate(self, prospects: list) -> list:
        """Research each company once (cached), park the unfit, keep the rest.

        A site that can't be read keeps its prospects: research only ever
        removes a company on evidence, never for lack of it."""
        by_company: dict[str, list] = {}
        for p in prospects:
            by_company.setdefault(p.company_id or "", []).append(p)
        kept = []
        for cid, group in by_company.items():
            company = None
            if cid:
                try:
                    company = await self.state.get_company(cid)
                except Exception:
                    company = None
            if company is None or not company.domain:
                kept.extend(group)
                continue
            profile = await company_research.cached(self.state, cid)
            if profile is None:
                try:
                    profile = await company_research.research_company(self.brain, company, self.config)
                except Exception as e:
                    logger.warning(f"Writer: research failed for {company.domain}: {e}")
                    profile = None
                if profile is None and getattr(self.brain, "paused_reason", ""):
                    # The provider is down: research and writing both wait.
                    logger.warning(f"Writer: {self.brain.paused_reason}; research waits.")
                    return []
                if profile is not None:
                    await company_research.save(self.state, cid, profile)
            self._research[cid] = profile
            if profile is None:
                logger.info(f"Writer: could not research {company.domain}; writing without it.")
                kept.extend(group)
                continue
            if profile["fit"] in ("reject", "weak"):
                for p in group:
                    await self.state.update_prospect_status(p.id, UNFIT_STATUS)
                if profile["fit"] == "reject":
                    try:
                        await self.state.add_suppression(
                            "domain", company.domain, "manual", source="research")
                    except Exception as e:
                        logger.warning(f"Writer: could not suppress {company.domain}: {e}")
                await self.state.log_action(
                    action_type="research_unfit", agent="writer",
                    details={"company": company.name, "domain": company.domain,
                             "fit": profile["fit"], "reason": profile.get("reason", ""),
                             "competitor": profile.get("competitor", False),
                             "prospects": [p.email for p in group]})
                logger.info(f"Writer: not writing to {company.domain} "
                            f"({profile['fit']} fit: {profile.get('reason', '')})")
                continue
            kept.extend(group)
        return kept

    async def _site_facts(self, company_id) -> list[str]:
        """Homepage findings for the prompt, unless writer.site_facts is off."""
        if not getattr(getattr(self.config, "writer", None), "site_facts", True):
            return []
        return await site_facts(getattr(self, "state", None), company_id)

    async def _has_tip_basis(self, prospect) -> bool:
        """Does the Writer have something concrete to build its one tip on?

        A homepage finding, a startup fact, a buying signal or the Scout's
        research notes. A failed read counts as "nothing known".
        """
        if (prospect.personalization_notes or "").strip():
            return True
        if await self._site_facts(prospect.company_id):
            return True
        if await self._startup(prospect.company_id):
            return True
        try:
            company = await self.state.get_company(prospect.company_id) if prospect.company_id else None
        except Exception:
            company = None
        return bool(company and company.signals)

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list,
                                        sequence: list | None = None):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if draft:
                pending.append((prospect, draft))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        drafted = 0
        for (prospect, _), draft in zip(pending, polished):
            names = _names(prospect) + self._profile_names(prospect)
            draft = await self._keep_to_rules(draft, step=1, names=names)
            if draft is None:
                continue
            if self._self_check_on() and self._profile(prospect):
                draft = await self._checked(prospect, draft, names)
                if draft is None:
                    await self.state.update_prospect_status(prospect.id, CHECK_FAILED_STATUS)
                    continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1
                if self._profile(prospect) and sequence:
                    await self._personal_follow_ups(
                        prospect, draft, sequence, campaign_id, names,
                        status=status, provider=provider)

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see sellbird.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _signal_guide(self, facts: str) -> str:
        """The parts of skills/signal_playbook.md that fit these facts ("" when
        none do, or the playbook is missing).

        The whole playbook covers hiring, funding, tools, search rank and new
        domains; one prospect has one or two of those, and the rest is noise
        in a prompt for a 40-word email."""
        load = getattr(self.brain, "load_skill", None)
        text = load("signal_playbook") if load else ""
        if not isinstance(text, str) or not text.strip():
            return ""
        sections = _sections(text)
        low = facts.lower()
        wanted = ["## The core rule"]
        for heading, words in _SIGNAL_SECTIONS:
            if any(w in low for w in words):
                wanted += [h for h in sections if h.startswith(heading)]
        if len(wanted) == 1:
            wanted.append("## When you have no real signal")
        body = "\n\n".join(f"{h}\n{sections[h]}" for h in dict.fromkeys(wanted) if h in sections)
        body = body.replace(" \u2014 ", ", ").replace("\u2014", ", ")
        return "\n\nUSING THEIR SIGNAL\n" + body if body else ""

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK: notes the person running Sellbird left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.location:
                facts.append(f"- Location: {company.location}")
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        facts.extend(await self._site_facts(prospect.company_id))
        profile = self._profile(prospect)
        if profile:
            facts.extend(company_research.fact_lines(profile))
            person = company_research.greeting_person(profile)
            if prospect.is_company_inbox and person:
                inbox_rule = (
                    "\n- This goes to the company's general inbox, but the research names "
                    f"{person['name']} ({person.get('role') or 'a manager'}). Greet them by name"
                    "\n  as the OPERATOR'S EMAIL RULES say for a known name (with a courtesy title only"
                    "\n  when the research gives it), or by full name if no rule says how.")
            if profile.get("language"):
                inbox_rule += (
                    f"\n- Write the whole email in {company_research.language_name(profile['language'])}, "
                    "the language of their website, even if other rules name another language.")
            inbox_rule += (
                "\n- Write no sentence about what you or your company do or how you work"
                "\n  (no 'We automate ...', 'We build ...'): the offer question is the only"
                "\n  line about you. Name the offer in a few plain words; don't list what it"
                "\n  contains.")
            if profile.get("hooks"):
                inbox_rule += (
                    "\n- Open with one of the 'Possible opening' facts. Never open with a sentence"
                    "\n  about their industry in general, and say nothing about their processes"
                    "\n  that the research does not support (hedge a guess as a guess).")
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(prospect.company_id)
        if startup is not None:
            facts.extend(startup)

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += self._signal_guide("\n".join(facts))
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += await self._feedback_block([prospect.id])
        rules = getattr(self, "email_rules", "")
        prompt += offer_block(self.config)
        prompt += operator_rules_block(rules)
        if rules:
            shape = (
                "- Follow the OPERATOR'S EMAIL RULES above for this first email: its length,\n"
                "  structure, what it says about the product, links and subject line.\n"
                "- Write the actual text (no merge variables: you know their name/company).")
            strict = "- Follow every STRICT EMAIL RULE above that the operator's rules don't replace."
        else:
            shape = PERSONAL_SHAPE
            strict = "- Follow every STRICT EMAIL RULE above."

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS: everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it. Do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{footer_rule(self.config, rules)}
{strict}{inbox_rule}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        self.__dict__.setdefault("_facts", {})[prospect.id] = "\n".join(facts)
        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        langs = {(self._profile(p) or {}).get("language", "") for p in prospects}
        lang_rule = ""
        if len(langs) == 1 and next(iter(langs)):
            lang_rule = (f"\n- Write all three emails in {company_research.language_name(next(iter(langs)))},"
                         "\n  the language of these companies' websites, even if other rules name another.")
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables. Every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += self._signal_guide(prospect_summary)
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])
        rules = getattr(self, "email_rules", "")
        prompt += offer_block(self.config)
        prompt += operator_rules_block(rules)
        if rules:
            shape = (
                "- Email 1 is the first touch, email 2 a follow-up 3 days later, email 3 a\n"
                "  last one 4 days after that. Follow the OPERATOR'S EMAIL RULES above for\n"
                "  each one's length, structure, what it says about the product, links and\n"
                "  subject line.")
            style = ""
            strict = ("- Follow every rule in the STRICT EMAIL RULES above that the operator's\n"
                      "  rules don't replace.")
        else:
            shape, style = SEQUENCE_SHAPE, "\n" + SEQUENCE_STYLE
            strict = "- Follow every rule in the STRICT EMAIL RULES above. No exceptions."

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{merge_rule}{style}{lang_rule}
{footer_rule(self.config, rules)}
{strict}

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result)
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        names = [n for p in prospects for n in _names(p)]
        for step, draft in zip(steps, polished):
            kept = await self._keep_to_rules(draft, step=step.step, names=names)
            if kept is None:
                # One template goes to the whole batch: a step that can't be
                # brought within the rules fails the sequence, and the batch
                # is tried again next cycle (counted, see MAX_WRITE_ATTEMPTS).
                return []
            step.subject, step.body = kept["subject"], kept["body"]
        return steps

    async def _personal_follow_ups(self, prospect, first: dict, sequence: list, campaign_id: str,
                                   names: list[str], *, status: str, provider: str) -> int:
        """Emails 2 and 3 written for this company from its research, staged
        next to its first email. The outbox keeps one row per (campaign,
        prospect, step), so the campaign template never replaces them; a step
        that can't be written well is left to the template. Returns how many."""
        if not getattr(getattr(self.config.channels, "email", None), "follow_ups", True):
            return 0
        later = [s for s in sequence if s.step > 1]
        if not later:
            return 0
        profile = self._profile(prospect) or {}
        lang = company_research.language_name(profile.get("language", ""))
        rules = getattr(self, "email_rules", "")
        greeting = next((ln.strip() for ln in str(first.get("body") or "").splitlines() if ln.strip()), "")
        self.__dict__.setdefault("_first_greeting", {})[prospect.id] = greeting
        steps_spec = ", ".join(
            f'{{"step": {s.step}, "subject": "...", "body": "..."}}' for s in later)
        prompt = f"""Write the follow-ups to this cold email, for when there is no reply.

THE FIRST EMAIL:
{fence(json.dumps(first, ensure_ascii=False, indent=2), "draft")}

RESEARCH (the only facts you may use about the company):
{fence(getattr(self, "_facts", {}).get(prospect.id, ""), "prospect research")}

{UNTRUSTED_RULE}

THE EMAIL RULES:
{rules or "(built-in: short, plain, one question, no pitch)"}

Requirements:
- {len(later)} emails, steps {", ".join(str(s.step) for s in later)}. Step 2 adds ONE new angle: a different
  fact from the research than the first email used, tied to the same free offer.
  The last step closes the loop politely in one or two sentences and leaves the offer open.
- Exactly the first email's greeting line ({greeting!r}), same language{(" (" + lang + ")") if lang else ""}, same sign-off.
- No sentence about what the sender's company does or how it works, and nothing
  about the company that is not in the research. Never "just following up".
- Write the actual text, no merge variables.

Return ONLY a JSON array: [{steps_spec}]"""
        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer", agent="writer", task="personal_follow_ups")
        if not isinstance(result, list):
            return 0
        written = {}
        for item in result:
            if isinstance(item, dict) and item.get("subject") and item.get("body"):
                try:
                    written[int(item.get("step"))] = item
                except (TypeError, ValueError):
                    continue
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        days, staged = 0, 0
        for s in later:
            days += max(0, s.delay_days)
            item = written.get(s.step)
            if item is None:
                continue
            draft = await self._keep_to_rules(
                {"subject": str(item["subject"]).strip()[:120], "body": str(item["body"]).strip()[:2000]},
                step=s.step, names=names)
            if draft is not None and self._self_check_on():
                draft = await self._checked(prospect, draft, names, step=s.step)
            if draft is None:
                logger.info(f"Writer: follow-up {s.step} for {prospect.email} left to the campaign template.")
                continue
            if await self.state.add_outbox_item(
                    prospect_id=prospect.id, campaign_id=campaign_id, step=s.step,
                    to_email=prospect.email, subject=draft["subject"], body=draft["body"],
                    send_at=(now + timedelta(days=days)).isoformat(), status=status,
                    provider=provider, approved_by="" if status == "pending_review" else "autopilot"):
                staged += 1
        return staged

    def _profile_names(self, prospect) -> list[str]:
        """Names the research found: text that is theirs, like their company name."""
        return [p["name"] for p in (self._profile(prospect) or {}).get("people") or [] if p.get("name")]

    async def _check_problems(self, prospect, draft: dict, step: int = 1) -> list[str] | None:
        """What is wrong with a researched first email, [] when nothing, None
        when the check itself failed (no reply)."""
        rules = getattr(self, "email_rules", "")
        profile = self._profile(prospect) or {}
        lang = company_research.language_name(profile.get("language", ""))
        person = company_research.greeting_person(profile)
        kind = "the first email" if step == 1 else f"follow-up email {step} (sent if there is no reply)"
        first_greeting = getattr(self, "_first_greeting", {}).get(prospect.id, "")
        if step > 1 and first_greeting:
            greeting_rule = f"Its greeting is not exactly the first email's greeting: {first_greeting!r}."
        elif person and prospect.is_company_inbox:
            greeting_rule = f"It does not greet {person['name']} by name."
        else:
            greeting_rule = "The greeting breaks the rules."
        prompt = f"""You check {kind} of a cold email sequence before it goes to a person's review queue.

RESEARCH about the recipient's company (from its own website):
{fence(getattr(self, "_facts", {}).get(prospect.id, ""), "prospect research")}

{UNTRUSTED_RULE}

THE EMAIL RULES:
{rules or "(built-in rules: short, one specific finding, one question, no pitch)"}

THE EMAIL (subject and body; Sellbird adds the postal address and opt-out line later):
{fence(json.dumps(draft, ensure_ascii=False, indent=2), "draft")}

List every problem from this checklist, and nothing else:
1. A claim about the company that the research does not support, stated as fact
   rather than hedged as a guess.
2. It opens with a sentence about the industry in general instead of this company.
3. It is not written in {lang or "the language the rules require"}.
4. {greeting_rule}
5. It breaks one of THE EMAIL RULES (length, one question, no links, no pitch, sign-off).
6. It describes what the company does wrongly.
7. It has a sentence describing what the sender's company does or how it works
   (a pitch).

Return ONLY JSON: {{"problems": ["...", "..."]}} (an empty list when it is fine)."""
        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer", agent="writer", task="self_check")
        if not isinstance(result, dict) or not isinstance(result.get("problems"), list):
            return None
        return [str(p).strip() for p in result["problems"] if str(p).strip()][:6]

    async def _checked(self, prospect, draft: dict, names: list[str], step: int = 1) -> dict | None:
        """``draft`` once it passes the self-check, after at most one rewrite;
        None when it still fails. A failed check call passes nothing."""
        problems = await self._check_problems(prospect, draft, step)
        if problems == []:
            return draft
        if problems is None:
            logger.warning(f"Writer: self-check gave no answer for {prospect.email}; not staging.")
            return None
        prompt = f"""Rewrite this cold email so it fixes exactly these problems:
{chr(10).join("- " + p for p in problems)}

RESEARCH (the only facts you may use about the company):
{fence(getattr(self, "_facts", {}).get(prospect.id, ""), "prospect research")}

{UNTRUSTED_RULE}

THE EMAIL RULES:
{getattr(self, "email_rules", "") or "(built-in: short, one finding, one question)"}

Draft:
{fence(json.dumps(draft, ensure_ascii=False, indent=2), "draft")}

Keep what is right, add no claim the research does not support, keep the sign-off.
Return ONLY a JSON object: {{"subject": "...", "body": "..."}}"""
        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer", agent="writer", task="self_check_fix")
        if not isinstance(result, dict) or not result.get("subject") or not result.get("body"):
            return None
        rewrite = await self._keep_to_rules(
            {"subject": str(result["subject"]).strip()[:120], "body": str(result["body"]).strip()[:2000]},
            step=step, names=names)
        if rewrite is None:
            return None
        if await self._check_problems(prospect, rewrite, step) == []:
            return rewrite
        logger.warning(f"Writer: draft for {prospect.email} failed its self-check twice: "
                       + "; ".join(problems))
        return None

    async def _keep_to_rules(self, draft: dict, *, step: int, names=()) -> dict | None:
        """``draft`` tidied, signed and within Sellbird's writing rules
        (sellbird.email_style), after at most one rewrite; None when even
        the rewrite breaks them.

        Operator email rules replace the built-in length and shape, and the
        sign-off (they may write their own); the always-rules (no dashes, no
        exclamation marks, no AI phrases, no formatting) still hold."""
        shape = not getattr(self, "email_rules", "")

        def finish(d: dict) -> dict:
            body = tidy(str(d.get("body") or ""))
            if shape:
                body = with_sign_off(body, self.config.persona.name)
            return {**d, "subject": tidy(str(d.get("subject") or "")), "body": body}

        def problems(d: dict) -> list[str]:
            return style_problems(d["subject"], d["body"], kind="cold", step=step,
                                  names=names, shape=shape)

        draft = finish(draft)
        found = problems(draft)
        if not found:
            return draft
        rewrite = await self._rewrite_to_rules(draft, found, step, shape)
        if rewrite is not None:
            rewrite = finish(rewrite)
            found = problems(rewrite)
            if not found:
                return rewrite
        logger.warning(f"Writer: dropped a draft for email {step} that breaks the rules: "
                       + "; ".join(found))
        return None

    async def _rewrite_to_rules(self, draft: dict, found: list[str], step: int,
                                shape: bool) -> dict | None:
        """One model call that fixes exactly what ``style_problems`` found."""
        body = draft["body"]
        if shape:
            # The sign-off is Sellbird's; the model never sees or writes it.
            first = (self.config.persona.name or "").strip().split(" ")[0]
            if first and body.endswith("\n\n" + first):
                body = body[: -len(first) - 2].rstrip()
        payload = {"subject": draft["subject"], "body": body}
        problems = "\n".join(f"- It {p}." for p in found)
        prompt = f"""This cold email (email {step} of a sequence) breaks Sellbird's writing rules:
{problems}

Rewrite it so it breaks none of them. Keep its facts, its one question and every
merge variable such as {{{{first_name}}}} exactly as written; add no fact, name or claim.
Make it shorter, never longer: cut words instead of adding them. Plain text, no
dash between words (use a period or a comma), no exclamation marks, no sign-off
and no name at the end.

Draft:
{fence(json.dumps(payload, ensure_ascii=False, indent=2), "draft")}

{UNTRUSTED_RULE}

Return ONLY a JSON object: {{"subject": "...", "body": "..."}}"""
        try:
            result = await self.brain.think_json(
                prompt, session_id="sellbird-writer", agent="writer", task="fix_draft",
            )
        except Exception as e:
            logger.warning(f"Writer: rewrite to rules failed: {e}")
            return None
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        new_body = str(result.get("body") or "").strip()
        if not subject or not new_body:
            return None
        before = set(MERGE_VAR.findall(payload["subject"] + payload["body"]))
        after = set(MERGE_VAR.findall(subject + new_body))
        if not after <= before or (before and not after):
            return None  # a merge variable appeared from nowhere, or all of them went
        return {**draft, "subject": subject[:120], "body": new_body[:2000]}

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{industry}-{kind}"
            # Researched: follow-ups are shared by the batch, so one language per batch.
            lang = (self._profile(prospect) or {}).get("language", "")
            if lang:
                key = f"{industry}-{lang}-{kind}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
