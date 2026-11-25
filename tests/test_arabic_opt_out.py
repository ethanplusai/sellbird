"""Arabic opt-outs are honoured and Latin accents fold in email local parts.

All people, businesses and addresses here are invented.
"""
import pytest

from sellbird.agents.handler import _escalates, _opt_out_reason
from sellbird.config import ComplianceConfig
from sellbird.integrations.email_finder import _clean_name_part, build_email


def test_arabic_opt_out_line_is_accepted():
    cfg = ComplianceConfig(opt_out_line="غير مهتم؟ فقط أخبرني ولن أراسلك مرة أخرى.")
    assert "لن أراسلك" in cfg.opt_out_line
    cfg = ComplianceConfig(opt_out_line="لإلغاء الاشتراك أرسل توقف")
    assert "توقف" in cfg.opt_out_line


def test_arabic_opt_out_line_with_a_link_is_rejected():
    with pytest.raises(ValueError, match="link"):
        ComplianceConfig(opt_out_line="لإلغاء الاشتراك اضغط هنا ولن أراسلك")


@pytest.mark.parametrize("reply", [
    "لا تراسلني مرة أخرى من فضلك",
    "توقفوا عن مراسلتي",
    "احذفني من القائمة",
    "إلغاء الاشتراك",
    "لا شكراً",
    "توقف شكراً",
    "\u202bتوقف\u202c",
    "تَوَقَّف",
])
def test_arabic_replies_are_opt_outs(reply):
    assert _opt_out_reason(reply, reply) is not None


@pytest.mark.parametrize("reply", [
    "مرحباً، هل يمكن إرسال عرض السعر؟",
    "نحن غير مهتمين بالمنتج الحالي لكن أخبرنا عن الخدمة الجديدة، ما سعرها؟",
])
def test_ordinary_arabic_replies_are_not_opt_outs(reply):
    assert _opt_out_reason(reply, reply) is None


def test_arabic_escalation_is_detected():
    assert _escalates("سأقاضيكم وأقدم شكوى رسمية") is True
    assert _escalates("مرحباً، هل يمكن إرسال عرض السعر؟") is False


def test_accented_names_fold_instead_of_losing_letters():
    assert _clean_name_part("Hélène") == "helene"
    assert _clean_name_part("Ömer") == "omer"
    assert _clean_name_part("Łukasz") == "lukasz"
    assert build_email("{f}.{l}", "Hélène", "Valès", "acme.example") == "helene.vales@acme.example"
