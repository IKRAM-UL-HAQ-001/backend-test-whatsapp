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
    cleaned = re.sub(r"[\s\-\(\)\[\]\{\}\.]", "", str(phone_number or ""))
    if not cleaned:
        return ""

    country_code = str(default_country_code or "+92")
    if not country_code.startswith("+"):
        country_code = f"+{country_code}"

    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("0"):
        return f"{country_code}{cleaned[1:]}"
    if cleaned.startswith("92"):
        return f"+{cleaned}"
    return cleaned
