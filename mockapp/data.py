"""Fixture data for the MeridianCore mock servicing console.

All values are synthetic. No real PII, no real account numbers, no real
credentials. Member ids are deliberately non-sequential so that "not found"
is easy to trigger without colliding with a real record.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_type: str
    account_number: str  # synthetic, masked in the UI as ****NNNN
    balance: str  # rendered exactly as the legacy UI renders it
    status: str
    opened: str


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    branch: str
    member_since: str
    status: str
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "100244": Member(
        member_id="100244",
        name="Dana Whitfield",
        branch="Riverbend Branch 014",
        member_since="2014-03-19",
        status="ACTIVE",
        accounts=[
            Account("Share Draft (Checking)", "****4417", "$1,208.55", "OPEN", "2014-03-19"),
            Account("Regular Savings", "****9031", "$4,812.37", "OPEN", "2014-03-19"),
            Account("Auto Loan", "****2260", "$11,430.02", "CURRENT", "2021-08-02"),
        ],
    ),
    "100731": Member(
        member_id="100731",
        name="Marcus Ellery",
        branch="Downtown Branch 002",
        member_since="2019-11-04",
        status="ACTIVE",
        accounts=[
            Account("Regular Savings", "****7714", "$129.05", "OPEN", "2019-11-04"),
            Account("Share Draft (Checking)", "****1180", "$62.90", "DORMANT", "2019-11-04"),
        ],
    ),
    "100999": Member(
        member_id="100999",
        name="Priya Raghunathan",
        branch="Executive Services 001",
        member_since="2008-01-22",
        status="ACTIVE",
        restricted=True,
        accounts=[
            Account("Regular Savings", "****3355", "$27,640.18", "OPEN", "2008-01-22"),
            Account("Money Market", "****8802", "$102,904.66", "OPEN", "2012-05-30"),
        ],
    ),
}

# The only credential the mock app accepts. It is a fixture, not a secret; the
# real value used at runtime is read from MOCK_APP_PASSWORD so that the demo
# still exercises the "credentials come from the environment" path.
DEFAULT_OPERATOR_USER = "ops.demo"
DEFAULT_OPERATOR_PASSWORD = "demo-not-a-real-password"

# Supervisor override code used by the human-in-the-loop demo.
SUPERVISOR_OVERRIDE_CODE = "OVR-4417"
