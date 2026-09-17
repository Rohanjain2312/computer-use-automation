"""A hand-written artifact equivalent to what discovery produces.

Tests for the replay engine must not depend on a model call, so this builds the
same capability by hand. It is also the readable reference for what a well-formed
artifact looks like.
"""

from __future__ import annotations

from cua.artifact.schema import (
    ActionKind,
    AppProfile,
    CapabilityArtifact,
    ExtractSpec,
    FailureRule,
    InputSpec,
    OutcomeDisposition,
    OutcomeRule,
    OutputSpec,
    Predicate,
    Provenance,
    RecoveryAction,
    RecoveryRule,
    RecoveryThen,
    RiskLevel,
    SafetyBinding,
    Sensitivity,
    Step,
    StrategyKind,
    SurfaceBinding,
    TargetPlan,
    TargetStrategy,
    TenantBinding,
    ValueRef,
    ValueType,
    WaitSpec,
    utc_now,
)


def _plan(description: str, role: str, name: str, *, label: str | None = None,
          frame: str | None = None) -> TargetPlan:
    strategies = [
        TargetStrategy(kind=StrategyKind.role_name, params={"role": role, "name": name},
                       confidence=0.94, rationale="role plus accessible name"),
    ]
    if label:
        strategies.append(
            TargetStrategy(kind=StrategyKind.label_proximity,
                           params={"role": role, "label": label}, confidence=0.87,
                           rationale="legacy label-to-the-left fallback"))
    strategies.append(
        TargetStrategy(kind=StrategyKind.text_anchor, params={"role": role, "text": name,
                                                              "match": "contains"},
                       confidence=0.6, rationale="visible-text fallback"))
    return TargetPlan(description=description, strategies=strategies, frame=frame)


def _cell_plan(description: str, row: str, column: str) -> TargetPlan:
    return TargetPlan(
        description=description,
        strategies=[
            TargetStrategy(
                kind=StrategyKind.table_cell,
                params={"headers": ["Type", "Account", "Current Balance", "Status", "Opened"],
                        "row_label": row, "column": column, "row_match": "ci_exact"},
                confidence=0.96, rationale="addressed by table semantics"),
            TargetStrategy(kind=StrategyKind.table_cell,
                           params={"row_label": row, "column": column, "row_match": "ci_exact"},
                           confidence=0.88, rationale="same addressing without the column set"),
        ],
        frame="acctframe",
    )


def _text(text: str, description: str) -> Predicate:
    return Predicate(kind="text_present", params={"text": text}, description=description)


def build_artifact() -> CapabilityArtifact:
    outcomes = [
        OutcomeRule(
            name="member_not_found",
            when=_text("No member matching that number was found",
                       "the console reports no such member"),
            disposition=OutcomeDisposition.return_to_caller,
            message="No member exists with the supplied member number.",
        ),
        OutcomeRule(
            name="permission_denied",
            when=_text("You do not have permission to view this member",
                       "the console refuses the operator's entitlement"),
            disposition=OutcomeDisposition.escalate_to_human,
            message="The servicing operator is not entitled to view this member record.",
            remediation="A supervisor must apply an override on this session.",
        ),
    ]
    failures = [
        FailureRule(name="host_transaction_error",
                    when=_text("SYSTEM ERROR", "the console returned a host error"),
                    error_class="application_error",
                    message="The servicing host returned an application error."),
    ]
    terminal = [o.when for o in outcomes] + [f.when for f in failures]

    def wait_any(expected: Predicate) -> WaitSpec:
        return WaitSpec(
            until=Predicate(kind="any_of", children=[expected] + terminal,
                            description=(expected.description or "") +
                            ", or a recognized outcome / error"),
            timeout_ms=15_000)

    cp_signon = _text("Operator Dashboard", "the dashboard confirms sign-on succeeded")
    cp_search = _text("Member Servicing — Inquiry", "the member inquiry screen is open")
    cp_detail = _text("Account Detail", "the member's account summary is open")
    cp_entry = Predicate(kind="element_present",
                         params={"strategy": "role_name",
                                 "params": {"role": "textbox", "name": "Operator ID"}},
                         description="the sign-on form is on screen")

    steps = [
        Step(id="s00_open", intent="Open the servicing console entry point",
             action=ActionKind.navigate, url="{{ config.base_url }}/",
             wait=wait_any(cp_entry), checkpoint=cp_entry, tags=["entry", "signon"]),
        Step(id="s01_operator_id", intent="Enter the servicing operator id",
             action=ActionKind.type,
             target=_plan("the Operator ID field", "textbox", "Operator ID", label="Operator ID"),
             value=ValueRef(input="operator_id"), tags=["signon"]),
        Step(id="s02_passcode", intent="Enter the operator passcode",
             action=ActionKind.type,
             target=_plan("the Passcode field", "textbox", "Passcode", label="Passcode"),
             value=ValueRef(input="operator_passcode"), tags=["signon"]),
        Step(id="s03_signon", intent="Sign on to the console",
             action=ActionKind.click, target=_plan("the Sign On button", "button", "Sign On"),
             risk=RiskLevel.reversible_write, wait=wait_any(cp_signon), checkpoint=cp_signon,
             tags=["signon"]),
        Step(id="s04_member_servicing", intent="Open Member Servicing from the navigation pane",
             action=ActionKind.click,
             target=_plan("the Member Servicing menu item", "button", "Member Servicing"),
             wait=wait_any(cp_search), checkpoint=cp_search),
        Step(id="s05_member_number", intent="Enter the member number to look up",
             action=ActionKind.type,
             target=_plan("the Member Number field", "textbox", "Member Number",
                          label="Member Number"),
             value=ValueRef(input="member_id")),
        Step(id="s06_search", intent="Run the member inquiry",
             action=ActionKind.click, target=_plan("the Search button", "button", "Search"),
             wait=wait_any(cp_detail), checkpoint=cp_detail),
    ]

    outputs = [
        OutputSpec(name="current_savings_balance", type=ValueType.decimal,
                   description="Current balance of the member's Regular Savings account.",
                   extract=ExtractSpec(
                       target=_cell_plan("the Current Balance cell of the Regular Savings row",
                                         "Regular Savings", "Current Balance"),
                       transforms=["strip", "money_to_decimal"])),
        OutputSpec(name="savings_account_status", type=ValueType.string,
                   description="Status of that Regular Savings account.",
                   extract=ExtractSpec(
                       target=_cell_plan("the Status cell of the Regular Savings row",
                                         "Regular Savings", "Status"),
                       transforms=["strip", "collapse_ws"])),
        OutputSpec(name="member_name", type=ValueType.string, sensitivity=Sensitivity.pii,
                   description="The member's name as shown on the member record.",
                   extract=ExtractSpec(
                       target=TargetPlan(
                           description="the value cell beside the 'Member Name' label",
                           strategies=[TargetStrategy(
                               kind=StrategyKind.label_proximity,
                               params={"role": "cell", "label": "Member Name"},
                               confidence=0.93,
                               rationale="key/value panel: the label sits to the left")],
                           frame="bodypane"),
                       transforms=["strip", "collapse_ws"])),
    ]

    success = Predicate(
        kind="all_of",
        children=[
            Predicate(kind="table_cell_matches",
                      params={"row_label": "Regular Savings", "column": "Current Balance",
                              "frame": "acctframe"},
                      description="the savings balance cell is present and non-empty"),
            cp_detail,
        ],
        description="the member's account summary is open and the savings balance is readable")

    return CapabilityArtifact(
        capability_id="member_savings_balance_lookup",
        version="1.0.0-fixture",
        name="Member savings balance lookup (test fixture)",
        description="Look up a member and return their Regular Savings balance and status.",
        surface=SurfaceBinding(
            entry_point="{{ config.base_url }}/",
            app_profile=AppProfile(vendor="Meridian Systems", product="MeridianCore Servicing",
                                   version_observed="7.2.1"),
            tenant=TenantBinding(tenant_id="riverbend_cu", variant="base")),
        safety=SafetyBinding(allowlist_profile="default",
                             max_autonomous_risk=RiskLevel.reversible_write),
        inputs=[
            InputSpec(name="operator_id", type=ValueType.string,
                      description="Servicing operator sign-on id.", source="environment",
                      env_var="MERIDIAN_OPERATOR_ID"),
            InputSpec(name="operator_passcode", type=ValueType.string,
                      description="Servicing operator passcode.",
                      sensitivity=Sensitivity.secret, source="environment",
                      env_var="MERIDIAN_OPERATOR_PASSCODE"),
            InputSpec(name="member_id", type=ValueType.string, description="Member number.",
                      pattern="[0-9]{6}", example="100244"),
        ],
        outputs=outputs,
        steps=steps,
        business_outcomes=outcomes,
        recovery_rules=[
            RecoveryRule(
                name="acknowledge_system_notice",
                when=_text("System Notice", "a maintenance interstitial is covering the screen"),
                actions=[RecoveryAction(
                    action="click",
                    target=_plan("the Acknowledge and Continue button", "button",
                                 "Acknowledge and Continue"))],
                description="The console shows a one-per-sign-on maintenance notice."),
            RecoveryRule(
                name="reauthenticate_after_session_expiry",
                when=_text("Your session has expired", "the console dropped the session"),
                actions=[],
                then=RecoveryThen.resume_from_step,
                resume_from_step="s00_open",
                max_attempts=2,
                description="An expired session invalidates everything the flow established, "
                            "so the run restarts from the entry point rather than retrying the "
                            "step that happened to notice."),
        ],
        failure_rules=failures,
        success_condition=success,
        provenance=Provenance(
            discovery_run_id="fixture", model="none (hand-written test fixture)", model_calls=0,
            created_at=utc_now(), goal="test fixture",
            transcript_sha256="sha256:0" * 1, transcript_evidence_path="tests/fixture_artifact.py",
            notes="hand-written; mirrors the shape discovery produces"),
    ).with_checksum()
