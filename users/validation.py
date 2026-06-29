import phonenumbers
from phonenumbers import NumberParseException
from rest_framework.exceptions import ValidationError
import re


def normalize_phone(country_code, phone_number):
    try:
        parsed = phonenumbers.parse(f"{country_code}{phone_number}", None)
    except NumberParseException as exc:
        raise ValidationError({"phone_number": str(exc)}) from exc

    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        raise ValidationError({"phone_number": "Invalid phone number"})

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    national = str(parsed.national_number)
    country = f"+{parsed.country_code}"
    return country, national, e164


def _region_for_country_code(default_country_code):
    """Map a dial code like "+92" to an ISO region ("PK") for libphonenumber."""
    cc_digits = re.sub(r"\D", "", str(default_country_code or "92")) or "92"
    try:
        region = phonenumbers.region_code_for_country_code(int(cc_digits))
    except (ValueError, TypeError):
        return "PK"
    # "ZZ" is libphonenumber's "unknown region" sentinel.
    return None if not region or region == "ZZ" else region


def normalize_contact_phone(phone_number, default_country_code="+92"):
    """Normalize an arbitrarily-formatted contact number to E.164 (+923435149587).

    Handles +/00 international prefixes, national numbers (via the default
    country region), spaces/dashes/brackets, and bare country-code numbers.
    Uses Google's libphonenumber for correctness and only falls back to the
    legacy heuristic if the parser can't make sense of the input, so a sync
    never silently drops a number.

    Reusable for other countries: pass e.g. default_country_code="+1".
    """
    raw = str(phone_number or "")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    has_leading_plus = raw.lstrip().startswith("+")
    region = _region_for_country_code(default_country_code)

    # Candidate parse inputs, most-specific first. (text, region_to_parse_with)
    candidates = []
    if has_leading_plus:
        candidates.append((f"+{digits}", None))
    elif digits.startswith("00"):
        # 00 is the international call prefix in many countries -> "+".
        candidates.append((f"+{digits[2:]}", None))
    else:
        # National number (e.g. 03435149587) interpreted via the default region,
        # then a bare international fallback (e.g. 923435149587 -> +92...).
        candidates.append((digits, region))
        candidates.append((f"+{digits}", None))

    for text, parse_region in candidates:
        try:
            parsed = phonenumbers.parse(text, parse_region)
        except NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    return _legacy_normalize_contact_phone(raw, default_country_code)


def _legacy_normalize_contact_phone(phone_number, default_country_code="+92"):
    """Best-effort heuristic used only when libphonenumber rejects the input."""
    raw = str(phone_number or "")
    has_leading_plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    # Treat a leading 00 international prefix as "+".
    if not has_leading_plus and digits.startswith("00"):
        has_leading_plus = True
        digits = digits[2:]
    cleaned = f"+{digits}" if has_leading_plus else digits
    if not cleaned:
        return ""

    country_code = str(default_country_code or "+92")
    if not country_code.startswith("+"):
        country_code = f"+{country_code}"
    country_digits = re.sub(r"\D", "", country_code)

    if cleaned.startswith("+"):
        return cleaned
    if country_digits == "92":
        if len(cleaned) == 11 and cleaned.startswith("03"):
            return f"+92{cleaned[1:]}"
        if len(cleaned) == 10 and cleaned.startswith("3"):
            return f"+92{cleaned}"
        if len(cleaned) == 12 and cleaned.startswith("92"):
            return f"+{cleaned}"
    if cleaned.startswith("0"):
        return f"{country_code}{cleaned[1:]}"
    if cleaned.startswith(country_digits):
        return f"+{cleaned}"
    return cleaned
