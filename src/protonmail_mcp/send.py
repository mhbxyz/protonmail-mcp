from __future__ import annotations

from collections.abc import Sequence

from .policy import SendPolicy


def _normalize(address: str) -> str:
    return address.strip().lower()


def _is_self(address: str, account: str) -> bool:
    normalized = _normalize(address)
    account_normalized = _normalize(account)
    if normalized == account_normalized:
        return True
    local, _, domain = account_normalized.partition("@")
    candidate_local, _, candidate_domain = normalized.partition("@")
    base_local = candidate_local.split("+", 1)[0]
    return base_local == local and candidate_domain == domain


def _allowed_domains(policy: SendPolicy) -> set[str]:
    return {item.strip().lower().lstrip("@.") for item in policy.allowed_domains}


def recipient_allowed(address: str, account: str, policy: SendPolicy) -> bool:
    normalized = _normalize(address)
    if policy.allow_self and _is_self(normalized, account):
        return True
    if normalized in {_normalize(item) for item in policy.allowed_recipients}:
        return True
    domain = normalized.partition("@")[2]
    return domain in _allowed_domains(policy)


def denied_recipients(
    addresses: Sequence[str], account: str, policy: SendPolicy
) -> list[str]:
    return [
        address for address in addresses if not recipient_allowed(address, account, policy)
    ]
