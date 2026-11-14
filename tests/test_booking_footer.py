from types import SimpleNamespace as NS

from sellbird.config import ComplianceConfig, compliance_footer_lines


def _cfg(flag, url="https://calendly.example/me/30min"):
    return NS(
        compliance=ComplianceConfig(postal_address="1 Example St", opt_out_line="Reply no thanks."),
        channels=NS(email=NS(booking_link_in_footer=flag)),
        product=NS(offer=NS(booking_url=url)),
    )


def test_footer_has_booking_link_first_when_on():
    assert compliance_footer_lines(_cfg(True)) == [
        "Pick a time that suits you: https://calendly.example/me/30min",
        "1 Example St", "Reply no thanks."]


def test_footer_unchanged_when_off_or_no_url():
    assert compliance_footer_lines(_cfg(False)) == ["1 Example St", "Reply no thanks."]
    assert compliance_footer_lines(_cfg(True, url="")) == ["1 Example St", "Reply no thanks."]
