"""RDP takes the account name and its scope as two fields, never one string.

Qualifying the username instead -- ``.\\Administrator`` for a member server,
``ENCON\\Administrator`` for a domain controller -- makes Windows search for an
account by that literal name. It answers STATUS_NO_SUCH_USER (0xC0000064)
*before* evaluating the password, and guacd relays only "Authentication failure
(invalid credentials?)". So a correct password reads as a wrong one, and the
Windows Security log is the only place the real reason appears.

That is not a hypothetical: remote desktop failed this way on every guest, and
the resemblance to a wrong password sent the investigation at the stored
credential -- which was correct the whole time, byte-identical to the recorded
image password.
"""

from app.core.console.credentials import ConsoleCredentials
from app.core.console.guacd import rdp_parameters


def _params(creds: ConsoleCredentials) -> dict[str, str]:
    return rdp_parameters(
        hostname="10.0.181.1",
        username=creds.username,
        domain=creds.domain,
        password=creds.password,
        width=1024,
        height=768,
    )


def test_the_username_guacd_receives_is_never_qualified() -> None:
    for creds in (
        ConsoleCredentials("Administrator", "pw", "Administrator (local)", "."),
        ConsoleCredentials("Administrator", "pw", "ENCON\\Administrator", "ENCON"),
    ):
        assert "\\" not in _params(creds)["username"]


def test_the_scope_travels_in_the_domain_field() -> None:
    local = _params(ConsoleCredentials("Administrator", "pw", "x", "."))
    assert local["domain"] == "."
    assert local["username"] == "Administrator"

    dc = _params(ConsoleCredentials("Administrator", "pw", "x", "ENCON"))
    assert dc["domain"] == "ENCON"
    assert dc["username"] == "Administrator"


def test_a_domain_is_always_sent_even_when_empty() -> None:
    # Omitting the key entirely is what the original defect did. guacd's `connect`
    # sends one value per element of its args list, so a parameter that is
    # sometimes absent is a different bug class again -- keep it always present.
    assert "domain" in _params(ConsoleCredentials("Administrator", "pw", "x", ""))


def test_the_label_keeps_the_qualified_form_for_humans() -> None:
    # The label is display-only and should stay the string someone would type at
    # a VM console; only the wire fields are split.
    creds = ConsoleCredentials("Administrator", "pw", "ENCON\\Administrator", "ENCON")
    assert creds.label == "ENCON\\Administrator"
    assert _params(creds)["username"] == "Administrator"


def test_a_ticket_predating_the_domain_field_still_builds_parameters() -> None:
    # Tickets live in Valkey across a deploy. The default keeps a 60-second-old
    # ticket redeemable instead of raising inside the socket handler.
    creds = ConsoleCredentials(username="Administrator", password="pw", label="x")
    assert creds.domain == ""
    assert _params(creds)["username"] == "Administrator"
