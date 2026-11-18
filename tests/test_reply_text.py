"""A reply reads as text everywhere: HTML becomes its words and an entity
such as &nbsp; the character it stands for, so the Conversations tab never
shows markup, and an outgoing draft never carries it."""

import base64
import json
from email.message import EmailMessage

from tests.test_dashboard_conversations import _sql
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from sellbird.email_style import style_problems, tidy
from sellbird.integrations.gmail import GmailProvider
from sellbird.integrations.mail_provider import clean_reply_text, html_to_text
from sellbird.integrations.smtp_mail import SmtpImapProvider
from sellbird.state import StateManager

HTML_REPLY = (
    '<html><head><style>p { color: red }</style></head><body>'
    '<div dir="ltr">Hi Sam,<br><br>Tuesday&nbsp;works.&nbsp; Fees &amp; terms?<br></div>'
    '<div class="gmail_quote">On Mon, Oct 12, Sam wrote:</div></body></html>'
)


def test_html_becomes_the_words_a_reader_sees():
    assert html_to_text(HTML_REPLY) == "Hi Sam,\n\nTuesday works. Fees & terms?\n\nOn Mon, Oct 12, Sam wrote:"


def test_plain_text_keeps_its_words_and_loses_only_entities():
    assert clean_reply_text("Sounds good.&nbsp; R&D at AT&T, 5 < 6 &#39;ok&#39;") == \
        "Sounds good.  R&D at AT&T, 5 < 6 'ok'"
    assert clean_reply_text("No thanks") == "No thanks"
    assert clean_reply_text(clean_reply_text(HTML_REPLY)) == html_to_text(HTML_REPLY)


def _gmail_html(html: str) -> dict:
    data = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    return {"mimeType": "text/html", "body": {"data": data},
            "headers": [{"name": "From", "value": "Pat <pat@example.com>"},
                        {"name": "Subject", "value": "Re: hello"}]}


def test_gmail_html_only_reply_has_no_entities():
    message = GmailProvider._parse_message({"id": "m1", "threadId": "t1",
                                            "payload": _gmail_html(HTML_REPLY)})
    assert "&nbsp;" not in message.body and "<" not in message.body
    assert message.body.startswith("Hi Sam,\n\nTuesday works.")


def test_gmail_snippet_is_decoded():
    payload = {"mimeType": "multipart/mixed", "parts": [],
               "headers": [{"name": "From", "value": "pat@example.com"}]}
    message = GmailProvider._parse_message({"id": "m2", "payload": payload,
                                            "snippet": "Let&#39;s talk&nbsp;Tuesday"})
    assert message.body == "Let's talk Tuesday"


def test_smtp_html_only_reply_has_no_entities():
    msg = EmailMessage()
    msg["From"] = "Pat <pat@example.com>"
    msg["Subject"] = "Re: hello"
    msg.set_content(HTML_REPLY, subtype="html")
    message = SmtpImapProvider._parse_rfc822(msg.as_bytes())
    assert message.body.startswith("Hi Sam,\n\nTuesday works. Fees & terms?")


def test_conversations_tab_shows_old_replies_without_entities(client):
    import asyncio
    asyncio.run(StateManager(str(client.db_path)).init_db())
    _sql(client.db_path, "INSERT INTO prospects (id, first_name, email, status) "
                         "VALUES ('p1', 'Pat', 'pat@example.com', 'replied')")
    thread = [{"sender": "prospect", "content": "Tuesday&nbsp;works.&nbsp;", "timestamp": "2026-10-18T10:00:00"},
              {"sender": "us", "content": "Great, see you then.", "timestamp": "2026-10-18T11:00:00"}]
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, thread_json, status) "
                         "VALUES ('c1', 'p1', ?, 'open')", (json.dumps(thread),))
    [convo] = client.get("/api/conversations").json()
    assert [m["content"] for m in convo["thread"]] == ["Tuesday works. ", "Great, see you then."]


def test_outgoing_drafts_never_carry_markup():
    assert tidy("Hi Pat,&nbsp;thanks") == "Hi Pat, thanks"
    assert any("HTML code" in p for p in style_problems("", "Fees &amp; terms are below.", kind="reply"))
    assert any("HTML code" in p for p in style_problems("", "Thanks<br>Sam", kind="reply"))
    assert not any("HTML code" in p for p in style_problems("", "R&D at AT&T works.", kind="reply"))
