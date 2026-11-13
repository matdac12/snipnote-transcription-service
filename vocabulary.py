"""Personal vocabulary for long-meeting transcription.

The speech APIs receive canonical spellings only. `heard_as` is for the
cleanup pass and for review. A global list can be added later; it is empty
on purpose. This is not the product glossary.
"""
from typing import Optional

MAX_TERMS = 100
MAX_TERM_CHARS = 50

# Mattia Da Campo. Other users get no personal terms until a vocabulary screen exists.
_MATTIA = "e6658fad-05e2-4d25-9779-857bc72bbc81"

# term, optional heard_as. Order is the order sent to the speech API.
_PERSONAL: dict[str, list[dict]] = {
    _MATTIA: [
        {"term": "JGalileo", "heard_as": [
            "Galileo", "Galilei", "G-Galileo", "J-Galileo", "J-Garileo", "JGallileo",
            "Gagli Leo", "Gigliardileo", "Gheghelileo", "GeGalileo", "Girodileo",
            "Galileoli", "Gia Galideo", "Gigalileo",
        ]},
        {"term": "Scienze Biologiche", "heard_as": ["senza biologiche"]},
        {"term": "Supabase", "heard_as": ["Superbase", "Pavese", "Probase", "Fabaseo"]},
        {"term": "Vercel", "heard_as": ["verce"]},
        {"term": "FisioDate", "heard_as": ["Fisiodate"]},
        {"term": "forfettario", "heard_as": ["fotofettario"]},
        {"term": "partita IVA", "heard_as": ["verità IVA", "qualità IVA"]},
        {"term": "DDT", "heard_as": ["dintiti"]},
        {"term": "PEC", "heard_as": ["pack finte"]},
        {"term": "Calendly", "heard_as": ["calendri"]},
        {"term": "Claude", "heard_as": ["Clod"]},
        {"term": "scannerizzare", "heard_as": ["scannalizzazione"]},
        {"term": "DocuSign"},
        {"term": "Business Central"},
        {"term": "WhatsApp"},
        {"term": "GitHub"},
        {"term": "OpenAI"},
        {"term": "Fable"},
        {"term": "Shopify"},
        {"term": "Odoo"},
        {"term": "HubSpot"},
        {"term": "Zapier"},
        {"term": "Dropbox"},
        {"term": "OneDrive"},
        {"term": "Linear"},
        {"term": "Stripe"},
        {"term": "Zoom"},
        {"term": "Aruba"},
        {"term": "Infocert"},
        {"term": "YourBrain"},
        {"term": "Soluzione Tasse"},
        {"term": "Osteo Easy"},
        {"term": "SIGAO"},
        {"term": "S400"},
        {"term": "Orange One"},
        {"term": "Molino Rachello"},
        {"term": "Rachello"},
        {"term": "Enasarco"},
        {"term": "Agenzia delle Entrate"},
        {"term": "nota di credito"},
        {"term": "pignorabile"},
        {"term": "anamnesi"},
        {"term": "INPS"},
        {"term": "OTP"},
        {"term": "IBAN"},
        {"term": "GDPR"},
        {"term": "POC"},
        {"term": "NAS"},
        {"term": "CRM"},
        {"term": "VPN"},
        {"term": "SQL"},
        {"term": "APS"},
    ],
}


def _clean_term(term: str) -> Optional[str]:
    text = " ".join((term or "").split())
    if not text or len(text) > MAX_TERM_CHARS:
        return None
    if any(character in text for character in "<>"):
        return None
    return text


def entries_for_user(user_id: Optional[str]) -> list[dict]:
    """Canonical entries for this user, capped at MAX_TERMS."""
    kept = []
    seen = set()
    for entry in _PERSONAL.get(str(user_id or ""), []):
        term = _clean_term(entry.get("term", ""))
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        heard = []
        for alias in entry.get("heard_as") or []:
            cleaned = _clean_term(alias)
            if cleaned and cleaned.casefold() != term.casefold():
                heard.append(cleaned)
        kept.append({"term": term, "heard_as": heard})
        if len(kept) == MAX_TERMS:
            break
    return kept


def terms_for_user(user_id: Optional[str]) -> list[str]:
    """Spellings to attach to a speech request. Mishearings are not included."""
    return [entry["term"] for entry in entries_for_user(user_id)]


def format_for_cleanup(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        heard = entry.get("heard_as") or []
        if heard:
            lines.append(f"- {entry['term']} (heard as: {', '.join(heard)})")
        else:
            lines.append(f"- {entry['term']}")
    return "\n".join(lines)
