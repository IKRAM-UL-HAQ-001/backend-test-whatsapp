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


def normalize_contact_phone(phone_number, default_country_code="+92"):
    raw = str(phone_number or "")
    has_leading_plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D", "", raw)
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
