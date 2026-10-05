from __future__ import annotations

from protonmail_mcp.policy import SendPolicy
from protonmail_mcp.send import denied_recipients, recipient_allowed


def base_policy(**overrides: object) -> SendPolicy:
    values: dict[str, object] = {
        "allow_self": True,
        "allowed_recipients": ("bob@ext.test",),
        "allowed_domains": ("corp.example",),
    }
    values.update(overrides)
    return SendPolicy(**values)  # type: ignore[arg-type]


def test_self_address_and_plus_addressing() -> None:
    policy = base_policy()
    assert recipient_allowed("me@proton.me", "me@proton.me", policy)
    assert recipient_allowed("me+tag@proton.me", "me@proton.me", policy)
    assert recipient_allowed("ME@PROTON.ME", "me@proton.me", policy)


def test_allowlists_are_case_insensitive() -> None:
    policy = base_policy()
    assert recipient_allowed("BOB@EXT.TEST", "me@proton.me", policy)
    assert recipient_allowed("x@corp.example", "me@proton.me", policy)
    assert recipient_allowed("x@CORP.EXAMPLE", "me@proton.me", policy)


def test_domain_match_is_exact_not_subdomain() -> None:
    policy = base_policy()
    assert not recipient_allowed("x@sub.corp.example", "me@proton.me", policy)
    assert not recipient_allowed("evil@other.test", "me@proton.me", policy)


def test_allow_self_can_be_disabled() -> None:
    policy = base_policy(allow_self=False)
    assert not recipient_allowed("me@proton.me", "me@proton.me", policy)


def test_denied_recipients_returns_offenders() -> None:
    policy = base_policy()
    denied = denied_recipients(
        ["me@proton.me", "bob@ext.test", "evil@other.test", "x@corp.example"],
        "me@proton.me",
        policy,
    )
    assert denied == ["evil@other.test"]
