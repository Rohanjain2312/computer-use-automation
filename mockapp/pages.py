"""Raw HTML for the mock console.

The markup here is intentionally hostile, in the way real back-office banking
software is hostile:

* top level is a ``<frameset>`` (not a layout div)
* the account panel is a nested same-origin ``<iframe>``
* layout is table-based with ``<font>``/``<center>`` and inline styles
* navigation items are ``<td onclick=...>``, not anchors
* form fields have no ``<label for=...>``; the label is the table cell to the left
* there are no test ids, and class names are opaque (``c1``, ``x2``)

Nothing in the automation stack is allowed to depend on this file's structure;
it exists to prove that perception works from what a human can see.
"""

from __future__ import annotations

from .data import STOP_PAYMENT_REASONS, Member

_HEAD = """<html><head><title>{title}</title>
<style>
body{{font-family:Tahoma,Arial,sans-serif;font-size:12px;background:#dfe3ea;margin:0}}
table.c1{{border-collapse:collapse}}
td.x2{{padding:3px 6px;border:1px solid #98a2b3}}
td.hd{{background:#1f3a5f;color:#fff;font-weight:bold;padding:4px 6px}}
.navitem{{padding:6px 8px;cursor:pointer;border-bottom:1px solid #b6bece}}
.navitem:hover{{background:#c8d4e8}}
.err{{color:#8a1c1c;font-weight:bold}}
input,select{{font-family:Tahoma,Arial;font-size:12px}}
</style></head><body>"""

_FOOT = "</body></html>"


def frameset() -> str:
    return (
        "<html><head><title>MeridianCore Servicing Console</title></head>"
        '<frameset cols="186,*" border="1" frameborder="1">'
        '<frame name="navpane" src="/nav" scrolling="no">'
        '<frame name="bodypane" src="/content">'
        "<noframes><body>This console requires frame support.</body></noframes>"
        "</frameset></html>"
    )


def nav(authenticated: bool) -> str:
    if not authenticated:
        items = ""
    else:
        items = "".join(
            f'<div class="navitem" onclick="parent.bodypane.location=\'{href}\'">{label}</div>'
            for label, href in [
                ("Dashboard", "/content"),
                ("Member Servicing", "/members"),
                ("Card Services", "/stub?m=Card+Services"),
                ("Batch Posting", "/stub?m=Batch+Posting"),
                ("Sign Off", "/logout"),
            ]
        )
    return (
        _HEAD.format(title="Nav")
        + '<div style="background:#1f3a5f;color:#fff;padding:6px 8px;font-weight:bold">MERIDIANCORE</div>'
        + '<div style="padding:4px 8px;color:#42506b">Servicing v7.2.1</div>'
        + items
        + _FOOT
    )


def login(error: str = "") -> str:
    err = f'<tr><td colspan="2"><span class="err">{error}</span></td></tr>' if error else ""
    return (
        _HEAD.format(title="Sign On")
        + '<center><table class="c1" width="430" style="margin-top:40px;background:#fff;border:2px solid #1f3a5f">'
        '<tr><td class="hd" colspan="2">MeridianCore &#8212; Operator Sign On</td></tr>'
        '<tr><td colspan="2" style="padding:6px"><font size="1" color="#42506b">'
        "Authorized use only. Activity is logged.</font></td></tr>"
        '<tr><td colspan="2"><form name="f1" method="post" action="/login">'
        '<table class="c1" width="100%">'
        '<tr><td class="x2" width="140">Operator ID</td>'
        '<td class="x2"><input type="text" name="opid" size="26"></td></tr>'
        '<tr><td class="x2">Passcode</td>'
        '<td class="x2"><input type="password" name="opsecret" size="26"></td></tr>'
        f"{err}"
        '<tr><td class="x2" colspan="2" align="right">'
        '<input type="submit" value="Sign On"></td></tr>'
        "</table></form></td></tr></table></center>" + _FOOT
    )


def dashboard(operator: str) -> str:
    # Legacy pattern: the content frame refreshes the nav frame after sign-on so
    # the menu reflects the new entitlements.
    return (
        _HEAD.format(title="Dashboard")
        + "<script>try{if(parent&&parent.navpane){parent.navpane.location='/nav';}}catch(e){}</script>"
        + '<table class="c1" width="100%"><tr><td class="hd">Operator Dashboard</td></tr></table>'
        + f'<div style="padding:10px">Signed on as <b>{operator}</b>.</div>'
        '<div style="padding:0 10px;color:#42506b">Select a function from the navigation pane.</div>'
        + _FOOT
    )


def system_notice(next_url: str) -> str:
    return (
        _HEAD.format(title="System Notice")
        + '<center><table class="c1" width="470" style="margin-top:60px;background:#fffbe6;border:2px solid #b08900">'
        '<tr><td class="hd" style="background:#b08900">System Notice</td></tr>'
        '<tr><td style="padding:12px">Scheduled maintenance window 02:00&#8211;03:00 CT. '
        "Batch posting may be delayed. This notice is shown once per sign-on.</td></tr>"
        f'<tr><td align="right" style="padding:8px"><form method="post" action="/notice/ack">'
        f'<input type="hidden" name="next" value="{next_url}">'
        '<input type="submit" value="Acknowledge and Continue"></form></td></tr>'
        "</table></center>" + _FOOT
    )


def session_expired() -> str:
    return (
        _HEAD.format(title="Session Ended")
        + '<center><table class="c1" width="440" style="margin-top:60px;background:#fff;border:2px solid #8a1c1c">'
        '<tr><td class="hd" style="background:#8a1c1c">Session Ended</td></tr>'
        '<tr><td style="padding:12px"><span class="err">Your session has expired.</span><br><br>'
        "Sign on again to continue.</td></tr>"
        '<tr><td align="right" style="padding:8px">'
        "<input type=\"button\" value=\"Return to Sign On\" onclick=\"location='/content'\">"
        "</td></tr></table></center>" + _FOOT
    )


def member_search(message: str = "") -> str:
    msg = (
        f'<tr><td colspan="2" style="padding:6px"><span class="err">{message}</span></td></tr>'
        if message
        else ""
    )
    return (
        _HEAD.format(title="Member Servicing")
        + '<table class="c1" width="100%"><tr><td class="hd">Member Servicing &#8212; Inquiry</td></tr></table>'
        '<table class="c1" style="margin:14px;background:#fff;border:1px solid #98a2b3" width="520">'
        '<tr><td colspan="2" style="padding:6px;background:#eef1f6"><b>Member Lookup</b></td></tr>'
        '<tr><td colspan="2"><form name="f1" method="post" action="/members/search">'
        '<table class="c1" width="100%">'
        '<tr><td class="x2" width="150">Member Number</td>'
        '<td class="x2"><input type="text" name="mbrid" size="18" maxlength="9"></td></tr>'
        '<tr><td class="x2">Inquiry Type</td><td class="x2">'
        '<select name="qtype"><option value="SUM">Account Summary</option>'
        '<option value="HIS">Transaction History</option></select></td></tr>'
        f"{msg}"
        '<tr><td class="x2" colspan="2" align="right">'
        '<input type="button" value="Search" onclick="document.f1.submit()">'
        "&nbsp;<input type=\"reset\" value=\"Clear\"></td></tr>"
        "</table></form></td></tr></table>" + _FOOT
    )


def permission_denied(member_id: str) -> str:
    return (
        _HEAD.format(title="Access Restricted")
        + '<table class="c1" width="100%"><tr><td class="hd">Member Servicing &#8212; Inquiry</td></tr></table>'
        '<center><table class="c1" width="520" style="margin-top:40px;background:#fff;border:2px solid #8a1c1c">'
        '<tr><td class="hd" style="background:#8a1c1c">Access Restricted</td></tr>'
        f'<tr><td style="padding:12px"><span class="err">You do not have permission to view this member.</span>'
        f'<br><br>Member {member_id} is flagged <b>EXECUTIVE SERVICES</b>. '
        "A supervisor override is required to continue.</td></tr>"
        '<tr><td align="right" style="padding:8px">'
        f"<input type=\"button\" value=\"Supervisor Override\" onclick=\"location='/override?mbr={member_id}'\">"
        "</td></tr></table></center>" + _FOOT
    )


def override_form(member_id: str, error: str = "") -> str:
    err = f'<tr><td colspan="2"><span class="err">{error}</span></td></tr>' if error else ""
    return (
        _HEAD.format(title="Supervisor Override")
        + '<table class="c1" width="100%"><tr><td class="hd">Supervisor Override</td></tr></table>'
        '<center><table class="c1" width="470" style="margin-top:36px;background:#fff;border:2px solid #1f3a5f">'
        '<tr><td class="hd" colspan="2">Entitlement Elevation</td></tr>'
        '<tr><td colspan="2" style="padding:8px"><font size="1" color="#42506b">'
        "A supervisor must enter the override code. This action is recorded.</font></td></tr>"
        '<tr><td colspan="2"><form name="f1" method="post" action="/override">'
        f'<input type="hidden" name="mbr" value="{member_id}">'
        '<table class="c1" width="100%">'
        f'<tr><td class="x2" width="160">Member Number</td><td class="x2">{member_id}</td></tr>'
        '<tr><td class="x2">Override Code</td>'
        '<td class="x2"><input type="password" name="ovrcode" size="20"></td></tr>'
        f"{err}"
        '<tr><td class="x2" colspan="2" align="right">'
        '<input type="submit" value="Apply Override"></td></tr>'
        "</table></form></td></tr></table></center>" + _FOOT
    )


def not_found(member_id: str) -> str:
    return (
        _HEAD.format(title="Member Servicing")
        + '<table class="c1" width="100%"><tr><td class="hd">Member Servicing &#8212; Inquiry</td></tr></table>'
        '<table class="c1" style="margin:14px;background:#fff;border:1px solid #98a2b3" width="520">'
        '<tr><td style="padding:10px"><span class="err">No member matching that number was found.</span>'
        f"<br><br>Searched member number: {member_id}</td></tr>"
        '<tr><td align="right" style="padding:8px">'
        "<input type=\"button\" value=\"New Search\" onclick=\"location='/members'\">"
        "</td></tr></table>" + _FOOT
    )


def member_detail(member: Member) -> str:
    return (
        _HEAD.format(title=f"Member {member.member_id}")
        + '<table class="c1" width="100%"><tr><td class="hd">Member Servicing &#8212; Account Summary</td></tr></table>'
        '<table class="c1" style="margin:12px;background:#fff;border:1px solid #98a2b3" width="640">'
        '<tr><td colspan="4" style="padding:5px;background:#eef1f6"><b>Member Record</b></td></tr>'
        f'<tr><td class="x2" width="130">Member Number</td><td class="x2" width="180">{member.member_id}</td>'
        f'<td class="x2" width="120">Member Status</td><td class="x2">{member.status}</td></tr>'
        f'<tr><td class="x2">Member Name</td><td class="x2">{member.name}</td>'
        f'<td class="x2">Member Since</td><td class="x2">{member.member_since}</td></tr>'
        f'<tr><td class="x2">Branch</td><td class="x2" colspan="3">{member.branch}</td></tr>'
        "</table>"
        '<div style="margin:12px"><b>Account Detail</b></div>'
        f'<iframe name="acctframe" src="/members/{member.member_id}/accounts" '
        'width="660" height="230" frameborder="1" style="margin:0 12px"></iframe>'
        + _servicing_actions(member.member_id)
        + _FOOT
    )


def _servicing_actions(member_id: str) -> str:
    """The servicing action bar on the member record.

    Three of these move money or close a record. They are here precisely so the
    allowlist and the risk classifier have something real to refuse: the
    automation walks past them on its way to the stop-payment form, and the
    policy gate — not the recording — is what keeps it from touching them.
    """
    cells = "".join(
        f'<td class="x2" style="cursor:pointer;background:#eef1f6" '
        f"onclick=\"location='{href}'\"><font size=\"2\">{label}</font></td>"
        for label, href in [
            ("Stop Payment", f"/members/{member_id}/stoppay"),
            ("Transfer Funds", f"/stub?m=Transfer+Funds"),
            ("Close Account", f"/stub?m=Close+Account"),
        ]
    )
    return (
        '<div style="margin:12px"><b>Servicing Actions</b></div>'
        '<table class="c1" style="margin:0 12px" width="640"><tr>' + cells + "</tr></table>"
    )


def accounts_frame(member: Member) -> str:
    rows = "".join(
        '<tr>'
        f'<td class="x2"><font size="2">{a.account_type}</font></td>'
        f'<td class="x2"><font size="2">{a.account_number}</font></td>'
        f'<td class="x2" align="right"><font size="2">{a.balance}</font></td>'
        f'<td class="x2"><font size="2">{a.status}</font></td>'
        f'<td class="x2"><font size="2">{a.opened}</font></td>'
        "</tr>"
        for a in member.accounts
    )
    return (
        _HEAD.format(title="Accounts")
        + '<table class="c1" width="100%" style="background:#fff">'
        '<tr><td class="hd">Type</td><td class="hd">Account</td><td class="hd">Current Balance</td>'
        '<td class="hd">Status</td><td class="hd">Opened</td></tr>'
        + rows
        + "</table>"
        + '<div style="padding:6px"><font size="1" color="#42506b">'
        "Balances are as of the last posted business day.</font></div>" + _FOOT
    )


def stop_payment_form(
    member: Member, error: str = "", values: dict[str, str] | None = None
) -> str:
    """The multi-field request form. Same hostile shape as the rest of the console.

    Labels are the table cell to the left, the draft list is a plain ``<select>``
    of the member's share-draft accounts, and the commit control sits next to a
    money-moving one so that telling them apart is the policy gate's job.
    """
    v = values or {}
    drafts = [a for a in member.accounts if "Draft" in a.account_type] or member.accounts
    options = "".join(
        f'<option value="{a.account_number}"'
        f'{" selected" if v.get("draft") == a.account_number else ""}>'
        f"{a.account_number} &#8212; {a.account_type}</option>"
        for a in drafts
    )
    reasons = "".join(
        f'<option value="{code}"{" selected" if v.get("reason") == code else ""}>{label}</option>'
        for code, label in STOP_PAYMENT_REASONS
    )
    err = (
        f'<tr><td class="x2" colspan="2"><span class="err">{error}</span></td></tr>'
        if error
        else ""
    )
    return (
        _HEAD.format(title="Stop Payment Request")
        + '<table class="c1" width="100%"><tr><td class="hd">'
        "Member Servicing &#8212; Stop Payment Request</td></tr></table>"
        '<table class="c1" style="margin:12px;background:#fff;border:1px solid #98a2b3" width="620">'
        '<tr><td colspan="2" style="padding:5px;background:#eef1f6"><b>Request Detail</b></td></tr>'
        '<tr><td colspan="2"><form name="f1" method="post" '
        f'action="/members/{member.member_id}/stoppay">'
        '<table class="c1" width="100%">'
        f'<tr><td class="x2" width="170">Member Number</td>'
        f'<td class="x2">{member.member_id}</td></tr>'
        f'<tr><td class="x2">Member Name</td><td class="x2">{member.name}</td></tr>'
        f'<tr><td class="x2">Draft Account</td>'
        f'<td class="x2"><select name="draft">{options}</select></td></tr>'
        f'<tr><td class="x2">Check Number</td><td class="x2">'
        f'<input type="text" name="cknum" size="12" maxlength="8" '
        f'value="{v.get("cknum", "")}"></td></tr>'
        f'<tr><td class="x2">Check Amount</td><td class="x2">'
        f'<input type="text" name="amount" size="14" value="{v.get("amount", "")}"></td></tr>'
        f'<tr><td class="x2">Reason</td>'
        f'<td class="x2"><select name="reason">{reasons}</select></td></tr>'
        f'<tr><td class="x2">Requested By</td><td class="x2">'
        f'<input type="text" name="reqby" size="24" value="{v.get("reqby", "")}"></td></tr>'
        f"{err}"
        '<tr><td class="x2" colspan="2" align="right">'
        '<input type="submit" value="Place Stop Payment">'
        "&nbsp;<input type=\"button\" value=\"Transfer Funds\" "
        "onclick=\"location='/stub?m=Transfer+Funds'\">"
        "&nbsp;<input type=\"button\" value=\"Cancel\" "
        f"onclick=\"location='/members/{member.member_id}'\"></td></tr>"
        "</table></form></td></tr></table>"
        '<div style="margin:0 12px"><font size="1" color="#42506b">'
        "A stop payment is effective for six months from the date recorded."
        "</font></div>" + _FOOT
    )


def stop_payment_confirmation(
    member: Member, reference: str, values: dict[str, str], reason_label: str
) -> str:
    """The checkpoint screen: the flow is only complete when this is on screen."""
    return (
        _HEAD.format(title="Stop Payment Confirmation")
        + '<table class="c1" width="100%"><tr><td class="hd">'
        "Member Servicing &#8212; Stop Payment Confirmation</td></tr></table>"
        '<center><table class="c1" width="560" '
        'style="margin-top:24px;background:#fff;border:2px solid #1f6f3a">'
        '<tr><td class="hd" colspan="2" style="background:#1f6f3a">'
        "Stop Payment Recorded</td></tr>"
        '<tr><td colspan="2" style="padding:8px"><font size="1" color="#42506b">'
        "The request has been accepted by the host. Give the reference below to the member."
        "</font></td></tr>"
        f'<tr><td class="x2" width="180">Reference Number</td>'
        f'<td class="x2"><b>{reference}</b></td></tr>'
        f'<tr><td class="x2">Request Status</td><td class="x2">RECORDED</td></tr>'
        f'<tr><td class="x2">Member Number</td><td class="x2">{member.member_id}</td></tr>'
        f'<tr><td class="x2">Draft Account</td><td class="x2">{values.get("draft", "")}</td></tr>'
        f'<tr><td class="x2">Check Number</td><td class="x2">{values.get("cknum", "")}</td></tr>'
        f'<tr><td class="x2">Check Amount</td><td class="x2">{values.get("amount", "")}</td></tr>'
        f'<tr><td class="x2">Reason</td><td class="x2">{reason_label}</td></tr>'
        f'<tr><td class="x2">Requested By</td><td class="x2">{values.get("reqby", "")}</td></tr>'
        '<tr><td align="right" style="padding:8px" colspan="2">'
        "<input type=\"button\" value=\"Return to Member\" "
        f"onclick=\"location='/members/{member.member_id}'\"></td></tr>"
        "</table></center>" + _FOOT
    )


def app_error(code: str) -> str:
    return (
        _HEAD.format(title="Application Error")
        + '<center><table class="c1" width="520" style="margin-top:50px;background:#fff;border:2px solid #8a1c1c">'
        '<tr><td class="hd" style="background:#8a1c1c">Application Error</td></tr>'
        f'<tr><td style="padding:12px"><span class="err">SYSTEM ERROR {code}</span><br><br>'
        "The host transaction could not be completed. Contact the service desk with "
        "this reference.</td></tr></table></center>" + _FOOT
    )


def stub(name: str) -> str:
    return (
        _HEAD.format(title=name)
        + f'<table class="c1" width="100%"><tr><td class="hd">{name}</td></tr></table>'
        '<div style="padding:12px">This function is not enabled for your operator profile.</div>'
        + _FOOT
    )
