"""Canonical technical-skill extraction for the reverse ETL.

`skills` on a crawled record is NOT a skills list — every parser fills it with
the bullet items of the posting's requirements section, so its values are
sentences, section headers and recruitment boilerplate. Feeding those into a
lookup table with UNIQUE(name) produced 13495 rows averaging 104 bytes, and one
6107-byte value aborted a whole sync.

This package replaces that with dictionary matching: only a name present in
`dictionary.CANONICAL` is ever stored, so an unrecognised value is dropped
rather than guessed at. Precision over recall, deterministic, no model calls.
"""

from .extractor import extract_skills, normalize  # noqa: F401
