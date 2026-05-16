"""License audit: detect copyleft (GPL/AGPL) deps in the install.

Murano's value proposition includes "MIT, no GPL imports." This module
introspects the installed Python packages via importlib.metadata and flags
anything in the GPL/AGPL family. Used by `murano licenses` and is suitable
for CI (exits non-zero on a copyleft hit).

License strings in Python package metadata are notoriously inconsistent —
some packages put "MIT", others "MIT License", others a URL, others SPDX
OR-expressions like `MPL-1.1 OR GPL-2.0-only`. We split on OR-separators
first; a package is only flagged as copyleft if EVERY alternative in the
expression is copyleft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import distributions

# Tokens that mark a copyleft license. Checked against each individual
# alternative in an OR-expression, not the full string.
COPYLEFT_TOKENS: tuple[str, ...] = (
    "agpl",
    "affero",
    "gplv",
    "gnu general public",
    "gpl-",
    "gpl ",
    "gpl3",
    "gpl2",
    "lgpl",
)

PERMISSIVE_HINTS: tuple[str, ...] = (
    "mit",
    "bsd",
    "apache",
    "isc",
    "psf",
    "mpl",
    "python software foundation",
    "unlicense",
    "0bsd",
    "cc0",
    "wtfpl",
    "zlib",
    "public domain",
    "boost",
)

# SPDX-style OR separator, plus a few common in the wild.
_ALT_SPLIT = re.compile(r"\s+OR\s+|\s+\|\s+|\s*/\s*", re.IGNORECASE)


@dataclass
class PackageLicense:
    name: str
    version: str
    license: str
    classifiers: list[str]
    copyleft: bool
    reason: str | None  # which token matched, or None if not copyleft


def _is_copyleft_alt(text: str) -> tuple[bool, str | None]:
    """Per-alternative classifier. `text` is one OR-clause."""
    lowered = text.lower()
    for tok in COPYLEFT_TOKENS:
        if tok in lowered:
            # If the alternative ALSO contains a non-copyleft hint adjacent
            # to nothing (e.g. "MIT") that's suspicious; flag as copyleft.
            return True, tok.strip()
    return False, None


def _classify_license_text(text: str) -> tuple[bool, str | None]:
    """Classify a full license expression.

    Multi-licensed packages (`MPL-1.1 OR GPL-2.0`) are permissive as long
    as ANY alternative is non-copyleft — the user is free to pick that one.
    Only flag as copyleft if EVERY alternative is copyleft.
    """
    if not text or not text.strip():
        return False, None
    alts = _ALT_SPLIT.split(text)
    alts = [a.strip() for a in alts if a.strip()]
    if not alts:
        return False, None
    copyleft_hits: list[str] = []
    permissive_seen = False
    for alt in alts:
        is_cl, reason = _is_copyleft_alt(alt)
        if is_cl:
            copyleft_hits.append(reason or alt)
        else:
            permissive_seen = True
    if permissive_seen:
        return False, None
    return True, copyleft_hits[0] if copyleft_hits else None


def _extract_license(meta) -> tuple[str, list[str]]:
    """Pull a best-effort license string + raw classifier list from metadata."""
    license_str = meta.get("License", "") or ""
    classifiers = meta.get_all("Classifier") or []
    # Some packages use PEP 639 "License-Expression" instead.
    expr = meta.get("License-Expression", "") or ""
    if expr:
        license_str = expr if not license_str else f"{license_str}; {expr}"
    return license_str.strip(), list(classifiers)


def audit() -> list[PackageLicense]:
    """Return one PackageLicense per installed distribution."""
    results: list[PackageLicense] = []
    for dist in distributions():
        meta = dist.metadata
        name = (meta.get("Name") or dist.metadata["Name"] or "").strip()
        version = (meta.get("Version") or "").strip()
        license_str, classifiers = _extract_license(meta)
        haystack = " ".join([license_str, *classifiers])
        copyleft, reason = _classify_license_text(haystack)
        results.append(
            PackageLicense(
                name=name,
                version=version,
                license=license_str or _best_classifier(classifiers),
                classifiers=classifiers,
                copyleft=copyleft,
                reason=reason,
            )
        )
    # De-duplicate by package name (some envs see the same dist twice).
    seen: dict[str, PackageLicense] = {}
    for r in results:
        if r.name and r.name.lower() not in seen:
            seen[r.name.lower()] = r
    return sorted(seen.values(), key=lambda x: x.name.lower())


def _best_classifier(classifiers: list[str]) -> str:
    for c in classifiers:
        if c.lower().startswith("license ::"):
            return c.split("::")[-1].strip()
    return "(unknown)"


def copyleft_packages(packages: list[PackageLicense]) -> list[PackageLicense]:
    return [p for p in packages if p.copyleft]
