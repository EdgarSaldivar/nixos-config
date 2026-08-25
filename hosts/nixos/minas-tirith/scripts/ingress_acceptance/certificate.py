"""Minimal stdlib-only DER decoding for observed TLS certificates."""

from __future__ import annotations

import datetime
from typing import Dict, Iterable, List, Tuple

from .models import CertificateIdentity


def _read_tlv(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    if offset >= len(data):
        raise ValueError("truncated DER tag")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("truncated DER length")
    first_length = data[offset]
    offset += 1
    if first_length & 0x80:
        length_bytes = first_length & 0x7F
        if length_bytes == 0 or offset + length_bytes > len(data):
            raise ValueError("invalid DER length")
        length = int.from_bytes(data[offset : offset + length_bytes], "big")
        offset += length_bytes
    else:
        length = first_length
    end = offset + length
    if end > len(data):
        raise ValueError("truncated DER value")
    return tag, data[offset:end], end


def _tlvs(data: bytes) -> Iterable[Tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        tag, value, offset = _read_tlv(data, offset)
        yield tag, value


def _decode_oid(data: bytes) -> str:
    values: List[int] = []
    current = 0
    for byte in data:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            values.append(current)
            current = 0
    if current or not values:
        raise ValueError("invalid DER object identifier")
    first = values.pop(0)
    if first < 40:
        arcs = [0, first]
    elif first < 80:
        arcs = [1, first - 40]
    else:
        arcs = [2, first - 80]
    arcs.extend(values)
    return ".".join(str(arc) for arc in arcs)


def _decode_der_string(tag: int, value: bytes) -> str:
    if tag == 0x1E:
        return value.decode("utf-16-be")
    if tag == 0x1C:
        return value.decode("utf-32-be")
    if tag == 0x0C:
        return value.decode("utf-8")
    return value.decode("latin-1")


def _parse_name(value: bytes) -> Dict[str, List[str]]:
    attributes: Dict[str, List[str]] = {}
    for set_tag, set_value in _tlvs(value):
        if set_tag != 0x31:
            continue
        for sequence_tag, sequence_value in _tlvs(set_value):
            if sequence_tag != 0x30:
                continue
            parts = list(_tlvs(sequence_value))
            if len(parts) < 2 or parts[0][0] != 0x06:
                continue
            oid = _decode_oid(parts[0][1])
            text = _decode_der_string(parts[1][0], parts[1][1])
            attributes.setdefault(oid, []).append(text)
    return attributes


def _display_name(attributes: Dict[str, List[str]], issuer: bool = False) -> str:
    common_name = (attributes.get("2.5.4.3") or [""])[0]
    organization = (attributes.get("2.5.4.10") or [""])[0]
    if issuer and organization and common_name and organization != common_name:
        return f"{organization} / {common_name}"
    if common_name:
        return common_name
    if organization:
        return organization
    return " / ".join(value for values in attributes.values() for value in values)


def _format_certificate_time(tag: int, value: bytes) -> str:
    text = value.decode("ascii")
    if not text.endswith("Z"):
        raise ValueError("certificate time is not UTC")
    if tag == 0x17 and len(text) == 13:
        short_year = int(text[0:2])
        year = 2000 + short_year if short_year <= 49 else 1900 + short_year
        remainder = text[2:]
    elif tag == 0x18 and len(text) == 15:
        year = int(text[0:4])
        remainder = text[4:]
    else:
        raise ValueError("unsupported certificate time format")
    try:
        parsed = datetime.datetime.strptime(
            f"{year:04d}{remainder}", "%Y%m%d%H%M%SZ"
        )
    except ValueError as exc:
        raise ValueError("invalid certificate time") from exc
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    return (
        f"{months[parsed.month - 1]} {parsed.day:2d} "
        f"{parsed:%H:%M:%S} {parsed.year:04d} GMT"
    )


def decode_certificate_identity(der: bytes) -> CertificateIdentity:
    outer_tag, outer_value, outer_end = _read_tlv(der)
    if outer_tag != 0x30 or outer_end != len(der):
        raise ValueError("certificate is not a DER sequence")
    outer_parts = list(_tlvs(outer_value))
    if not outer_parts or outer_parts[0][0] != 0x30:
        raise ValueError("certificate has no TBSCertificate")
    tbs_parts = list(_tlvs(outer_parts[0][1]))
    index = 1 if tbs_parts and tbs_parts[0][0] == 0xA0 else 0
    if len(tbs_parts) < index + 6:
        raise ValueError("truncated TBSCertificate")
    issuer_tag, issuer_value = tbs_parts[index + 2]
    validity_tag, validity_value = tbs_parts[index + 3]
    subject_tag, subject_value = tbs_parts[index + 4]
    if issuer_tag != 0x30 or validity_tag != 0x30 or subject_tag != 0x30:
        raise ValueError("invalid certificate identity fields")
    validity = list(_tlvs(validity_value))
    if len(validity) != 2 or validity[1][0] not in (0x17, 0x18):
        raise ValueError("invalid certificate validity")
    not_after = _format_certificate_time(validity[1][0], validity[1][1])

    sans: List[str] = []
    for tag, extension_wrapper in tbs_parts[index + 6 :]:
        if tag != 0xA3:
            continue
        wrapper = list(_tlvs(extension_wrapper))
        if len(wrapper) != 1 or wrapper[0][0] != 0x30:
            continue
        for extension_tag, extension_value in _tlvs(wrapper[0][1]):
            if extension_tag != 0x30:
                continue
            extension_parts = list(_tlvs(extension_value))
            if not extension_parts or extension_parts[0][0] != 0x06:
                continue
            if _decode_oid(extension_parts[0][1]) != "2.5.29.17":
                continue
            octet_strings = [value for part_tag, value in extension_parts if part_tag == 0x04]
            if not octet_strings:
                continue
            san_tag, san_value, san_end = _read_tlv(octet_strings[-1])
            if san_tag != 0x30 or san_end != len(octet_strings[-1]):
                raise ValueError("invalid subjectAltName extension")
            for name_tag, name_value in _tlvs(san_value):
                if name_tag == 0x82:
                    sans.append(name_value.decode("ascii"))

    return CertificateIdentity(
        issuer=_display_name(_parse_name(issuer_value), issuer=True),
        subject=_display_name(_parse_name(subject_value)),
        not_after=not_after,
        sans=tuple(sans),
    )
