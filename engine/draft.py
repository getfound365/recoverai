"""Agent 2 - Compliance Draftsman.

Turns one normalized record into a firm, professional past-due notice. The disclaimer
required by the rulebook is appended as the LAST content, byte-identical, exactly once.
A generated notice is never edited afterwards: whatever the human approves is what the
orchestrator sends.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from . import clock, config


def client_directory() -> Dict[str, Any]:
    return config.load_json(config.config_dir() / "clients.json")["clients"]


def client_profile(client_id: str) -> Dict[str, Any]:
    directory = client_directory()
    return directory.get(client_id, {
        "client_id": client_id,
        "legal_name": client_id,
        "contact_email": "accounts@example.invalid",
        "contact_phone": "(000) 000-0000",
        "billing_contact_name": "Accounts Receivable",
    })


def _money(amount: Optional[float]) -> str:
    return "an amount to be confirmed" if amount is None else f"${amount:,.2f}"


def _aging_phrase(aging_state: str, age_days: Optional[int]) -> str:
    if aging_state == "unknown":
        return "past due (invoice date could not be read from the ledger export)"
    if aging_state == "future":
        return "dated in the future on our ledger"
    if aging_state == "current":
        return "not yet past due"
    if age_days:
        return f"{age_days} days past due ({aging_state}+ day aging bucket)"
    return f"in the {aging_state}+ day aging bucket"


SUBJECT = "Past-due balance on invoice {invoice_number}"


def render(record: Dict[str, Any], channel: str = "email", approved_by: Optional[str] = None) -> Dict[str, Any]:
    """Render the notice for one debtor+channel. Returns the artifact we store and send."""
    profile = client_profile(record["client_id"])
    invoice = record.get("invoice_number") or record.get("debtor_id")
    amount = _money(record.get("balance_owed"))
    aging = _aging_phrase(record.get("aging_state", "unknown"), record.get("age_days"))
    name = record.get("name") or "Customer"
    invoice_date = record.get("invoice_date") or "an unknown date"

    header = (
        f"{profile['legal_name']}\n"
        f"{profile['billing_contact_name']}\n"
        f"{profile['contact_email']} | {profile['contact_phone']}\n"
        f"{clock.iso()}\n"
    )

    if channel in ("voice", "sms"):
        body = (
            f"Hello, this message is for {name}. This is {profile['billing_contact_name']} calling from "
            f"{profile['legal_name']} about invoice {invoice} for {amount}, which our records show is "
            f"{aging}. We would like to arrange payment. Please call us back at {profile['contact_phone']} "
            f"or reply to {profile['contact_email']} with your expected payment date. If payment has "
            f"already been sent, thank you - please disregard this message."
        )
    else:
        body = (
            f"{header}\n"
            f"Dear {name},\n\n"
            f"Our records show that invoice {invoice}, issued {invoice_date}, for {amount}, is {aging}.\n\n"
            f"We are asking for payment, or a payment date we can rely on. Please arrange settlement of the "
            f"outstanding balance, or reply to {profile['contact_email']} or call {profile['contact_phone']} "
            f"with the date you expect to pay. If the balance is already settled, or the invoice has been "
            f"paid recently, please let us know and we will update our ledger.\n\n"
            f"Thank you,\n"
            f"{profile['billing_contact_name']}\n"
            f"{profile['legal_name']}"
        )

    text = _append_disclaimer(body)

    return {
        "debtor_id": record["debtor_id"],
        "client_id": record["client_id"],
        "channel": channel,
        "subject": SUBJECT.format(invoice_number=invoice),
        "body": text,
        "disclaimer": config.disclaimer(),
        "disclaimer_occurrences": text.count(config.disclaimer()),
        "rulebook_version": config.load_rulebook()["rulebook_version"],
        "authorization_document_version": config.authorization_document()["version"],
        "generated_at": clock.iso(),
        "approved_by": approved_by,
        "aging_state": record.get("aging_state"),
        "balance_owed": record.get("balance_owed"),
    }


def _append_disclaimer(body: str) -> str:
    """Append the rulebook disclaimer verbatim, once, as the absolute last content."""
    text = body.rstrip("\n")
    if config.disclaimer() in text:
        # never allow a second copy: strip any pre-existing occurrence before appending
        text = text.replace(config.disclaimer(), "").rstrip("\n")
    return f"{text}\n\n{config.disclaimer()}"


def disclaimer_is_last(body: str) -> bool:
    return body.endswith(config.disclaimer())


def disclaimer_occurrences(body: str) -> int:
    return body.count(config.disclaimer())


def draft_filename(draft: Dict[str, Any]) -> str:
    return f"{draft['client_id']}__{draft['debtor_id']}__{draft['channel']}.txt"


def draft_text(draft: Dict[str, Any]) -> str:
    lines = [
        f"# RecoverAI draft - {draft['debtor_id']} / {draft['client_id']} / {draft['channel']}",
        f"# generated_at: {draft['generated_at']}  rulebook: {draft['rulebook_version']}",
        f"# aging_state: {draft['aging_state']}  balance_owed: {draft['balance_owed']}",
        f"# disclaimer_occurrences: {draft['disclaimer_occurrences']} "
        f"(must be exactly {config.disclaimer_versions()})",
        f"SUBJECT: {draft['subject']}",
        "",
        draft["body"],
        "",
    ]
    return "\n".join(lines)


def age_label(record: Dict[str, Any]) -> str:
    state = record.get("aging_state", "unknown")
    for bucket in config.aging_buckets():
        if bucket["state"] == state:
            return bucket["label"]
    return config.load_rulebook()["aging_edge_states"].get(state, state)
