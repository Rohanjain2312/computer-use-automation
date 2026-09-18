"""MeridianCore — a mock legacy credit-union servicing console.

Stands in for the kind of back-office application this project targets: no API,
frameset layout, table markup, no test ids. It also exposes a small fault
injection surface (``/admin/inject``) so that replay error handling can be
demonstrated deterministically instead of waiting for a real outage.
"""

from __future__ import annotations

import os
import re
import time

from flask import Flask, Response, redirect, request, session

from . import pages
from .data import (
    DEFAULT_OPERATOR_PASSWORD,
    DEFAULT_OPERATOR_USER,
    MEMBERS,
    STOP_PAYMENT_REASONS,
    SUPERVISOR_OVERRIDE_CODE,
    stop_payment_reference,
)

#: What the host accepts as dollars and cents, with or without $ and separators.
_AMOUNT = re.compile(r"^\$?\d{1,3}(,\d{3})*(\.\d{2})?$|^\$?\d+(\.\d{2})?$")

# Fault modes that /admin/inject understands.
FAULT_NONE = "none"
FAULT_APP_ERROR = "app_error"  # member detail returns a host error page
FAULT_SLOW = "slow"  # account frame takes ~9s to respond
FAULT_EXPIRE = "expire"  # next authenticated request reports session expiry

_state = {"fault": FAULT_NONE}


def _html(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/html")


def _operator_password() -> str:
    return os.environ.get("MOCK_APP_PASSWORD", DEFAULT_OPERATOR_PASSWORD)


def _operator_user() -> str:
    return os.environ.get("MOCK_APP_USER", DEFAULT_OPERATOR_USER)


def create_app() -> Flask:
    app = Flask(__name__)
    # Fixture key: this app holds no real data and is bound to localhost.
    app.secret_key = os.environ.get("MOCK_APP_SECRET", "meridiancore-mock-session-key")

    def authed() -> bool:
        return bool(session.get("op"))

    def expired_check() -> Response | None:
        """Consume a one-shot injected session expiry."""
        if _state["fault"] == FAULT_EXPIRE and authed():
            session.clear()
            _state["fault"] = FAULT_NONE
            return _html(pages.session_expired())
        if not authed():
            return _html(pages.login())
        return None

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "fault": _state["fault"]}

    @app.get("/")
    def index():
        return _html(pages.frameset())

    @app.get("/nav")
    def nav():
        return _html(pages.nav(authed()))

    @app.get("/content")
    def content():
        if not authed():
            return _html(pages.login())
        return _html(pages.dashboard(session["op"]))

    @app.post("/login")
    def login():
        opid = (request.form.get("opid") or "").strip()
        secret = request.form.get("opsecret") or ""
        if opid == _operator_user() and secret == _operator_password():
            session.clear()
            session["op"] = opid
            session["notice_ack"] = False
            return _html(pages.dashboard(opid))
        return _html(pages.login("Sign on failed. Check operator id and passcode."))

    @app.get("/logout")
    def logout():
        session.clear()
        return _html(pages.login())

    @app.get("/members")
    def members():
        guard = expired_check()
        if guard is not None:
            return guard
        if not session.get("notice_ack"):
            return _html(pages.system_notice("/members"))
        return _html(pages.member_search())

    @app.post("/notice/ack")
    def notice_ack():
        session["notice_ack"] = True
        return redirect(request.form.get("next") or "/members")

    @app.post("/members/search")
    def member_search():
        guard = expired_check()
        if guard is not None:
            return guard
        member_id = (request.form.get("mbrid") or "").strip()
        if not member_id:
            return _html(pages.member_search("Member number is required."))
        if not member_id.isdigit():
            return _html(pages.member_search("Member number must be numeric."))
        member = MEMBERS.get(member_id)
        if member is None:
            return _html(pages.not_found(member_id))
        return redirect(f"/members/{member_id}")

    @app.get("/members/<member_id>")
    def member_detail(member_id: str):
        guard = expired_check()
        if guard is not None:
            return guard
        member = MEMBERS.get(member_id)
        if member is None:
            return _html(pages.not_found(member_id))
        if member.restricted and member_id not in session.get("overrides", []):
            return _html(pages.permission_denied(member_id))
        if _state["fault"] == FAULT_APP_ERROR:
            return _html(pages.app_error("MC-5001"), status=500)
        return _html(pages.member_detail(member))

    @app.get("/members/<member_id>/accounts")
    def member_accounts(member_id: str):
        guard = expired_check()
        if guard is not None:
            return guard
        member = MEMBERS.get(member_id)
        if member is None:
            return _html(pages.not_found(member_id))
        if member.restricted and member_id not in session.get("overrides", []):
            return _html(pages.permission_denied(member_id))
        if _state["fault"] == FAULT_SLOW:
            time.sleep(float(os.environ.get("MOCK_APP_SLOW_SECONDS", "9")))
        return _html(pages.accounts_frame(member))

    @app.get("/members/<member_id>/stoppay")
    def stop_payment_get(member_id: str):
        guard = expired_check()
        if guard is not None:
            return guard
        member = MEMBERS.get(member_id)
        if member is None:
            return _html(pages.not_found(member_id))
        if member.restricted and member_id not in session.get("overrides", []):
            return _html(pages.permission_denied(member_id))
        return _html(pages.stop_payment_form(member))

    @app.post("/members/<member_id>/stoppay")
    def stop_payment_post(member_id: str):
        guard = expired_check()
        if guard is not None:
            return guard
        member = MEMBERS.get(member_id)
        if member is None:
            return _html(pages.not_found(member_id))
        if member.restricted and member_id not in session.get("overrides", []):
            return _html(pages.permission_denied(member_id))

        values = {k: (request.form.get(k) or "").strip()
                  for k in ("draft", "cknum", "amount", "reason", "reqby")}
        reasons = dict(STOP_PAYMENT_REASONS)

        # Field validation, in the order the legacy host checks it. Each of these
        # is a real runtime condition a replay has to tell apart from a fault.
        if not values["cknum"]:
            return _html(pages.stop_payment_form(
                member, "Check number is required.", values))
        if not values["cknum"].isdigit():
            return _html(pages.stop_payment_form(
                member, "Check number must be numeric.", values))
        if not values["amount"]:
            return _html(pages.stop_payment_form(
                member, "Check amount is required.", values))
        if not _AMOUNT.match(values["amount"]):
            return _html(pages.stop_payment_form(
                member, "Check amount must be entered as dollars and cents.", values))
        if not values["reqby"]:
            return _html(pages.stop_payment_form(
                member, "Requested By is required.", values))
        if values["reason"] not in reasons:
            return _html(pages.stop_payment_form(
                member, "Select a reason for the stop payment.", values))

        if _state["fault"] == FAULT_APP_ERROR:
            return _html(pages.app_error("MC-5001"), status=500)

        reference = stop_payment_reference(member_id, values["cknum"])
        session["stop_payments"] = sorted(
            set(session.get("stop_payments", [])) | {reference})
        return _html(pages.stop_payment_confirmation(
            member, reference, values, reasons[values["reason"]]))

    @app.get("/override")
    def override_get():
        guard = expired_check()
        if guard is not None:
            return guard
        return _html(pages.override_form(request.args.get("mbr", "")))

    @app.post("/override")
    def override_post():
        guard = expired_check()
        if guard is not None:
            return guard
        member_id = request.form.get("mbr", "")
        if (request.form.get("ovrcode") or "") != SUPERVISOR_OVERRIDE_CODE:
            return _html(pages.override_form(member_id, "Override code rejected."))
        session["overrides"] = sorted(set(session.get("overrides", [])) | {member_id})
        return redirect(f"/members/{member_id}")

    @app.get("/stub")
    def stub():
        return _html(pages.stub(request.args.get("m", "Function")))

    @app.route("/admin/inject", methods=["GET", "POST"])
    def inject():
        mode = (request.values.get("mode") or FAULT_NONE).strip()
        if mode not in {FAULT_NONE, FAULT_APP_ERROR, FAULT_SLOW, FAULT_EXPIRE}:
            return {"ok": False, "error": f"unknown mode {mode!r}"}, 400
        _state["fault"] = mode
        return {"ok": True, "fault": mode}

    return app


def main() -> None:
    host = os.environ.get("MOCK_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_APP_PORT", "8799"))
    create_app().run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
