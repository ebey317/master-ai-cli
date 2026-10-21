"""Strip leaked <arg_key>/<arg_value> XML noise from directive payloads.

Small models trained on native tool-call XML formats (Hermes-style
``<arg_key>name</arg_key><arg_value>payload</arg_value>``) sometimes glue
their own tool-call markup onto a directive payload instead of emitting
the app's plain ``KEYWORD: payload`` grammar. Two shapes show up live:

**Trailing noise** -- the real payload comes first and the XML follows:

    RUN: ls foo</arg_value><arg_key>description</arg_key>

The payload is ``ls foo``; everything from the first tag onward is noise.
This shape has always worked: the parser truncated at the first tag.

**Wrapping noise** -- the model wraps the ENTIRE payload in its tags:

    RUN: <arg_value>ls foo</arg_value>

The old truncate-at-first-tag logic assumed the tag always trailed
*after* real content, so truncating "everything before the first tag"
wiped the whole payload to ``""``. The directive then silently vanished:
``has_directives`` is already True (RUN has a colon), so the
malformed-directive stall detector -- which only fires when it is False --
never saw it either. Reproduced live against the current dispatch path:
a wrapped ``RUN:`` produced zero executions, with no error and no repair
feedback to the model.

The wrapping shape also comes as a key/value PAIR, where the key names
the parameter and the value holds the actual content:

    RUN: <arg_key>command</arg_key><arg_value>ls foo</arg_value>

Here the payload is the ``<arg_value>`` content -- the ``<arg_key>`` is
the parameter name ("command"), not the payload. Picking the wrong one
would run the literal word "command".

So this module resolves a payload to its real content by walking the
XML-ish structure rather than truncating at the first tag:

- content before the first tag wins (trailing-noise shape),
- otherwise the first ``<arg_value>`` element's content wins (the pair
  shape -- the value is the payload),
- otherwise the first ``<arg_key>`` element's content wins,
- and if the payload is entirely wrapped with no inner content, the
  result is empty rather than the raw tag text.

Pure string handling, no I/O, no dependency on the agent loop -- the same
discipline the other extracted seam modules follow. Importable from both
``dispatch.py`` and ``context.py`` without an import cycle: this module
imports nothing from the project at all.
"""

from __future__ import annotations

import re

__all__ = ["strip_arg_xml_noise"]


# Any tag for either name -- opening OR closing. This is what bounds the
# "content before the first tag" region: a closing tag ends the real
# content just as firmly as an opening one does.
_ARG_ANY_TAG_RE = re.compile(r"</?\s*arg_(?:value|key)\b", re.IGNORECASE)

# Opening tag for either name, tolerating whitespace and attributes:
#   <arg_value>, <arg_value >, <arg_value name="x">
_ARG_OPEN_RE = re.compile(r"<\s*arg_(?:value|key)\b[^>]*>", re.IGNORECASE)

# A complete element with its content: <arg_value>CONTENT</arg_value>
_ARG_ELEMENT_RE = re.compile(
    r"<\s*arg_(value|key)\b[^>]*>(.*?)<\s*/\s*arg_\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# A complete element whose content is empty.
_ARG_EMPTY_ELEMENT_RE = re.compile(
    r"<\s*arg_(value|key)\b[^>]*>\s*<\s*/\s*arg_\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def strip_arg_xml_noise(payload: str) -> str:
    """Return the real directive payload with leaked arg XML removed.

    ``payload`` is the text after a directive's colon. The result is the
    content the directive should actually act on -- or ``""`` when the
    payload was nothing but tags.
    """
    if not payload:
        return payload

    # 0. No tags at all -- the payload is already clean.
    first_tag = _ARG_ANY_TAG_RE.search(payload)
    if first_tag is None:
        return payload

    # 1. Content before the first tag is the payload (trailing-noise
    #    shape). This is the common case and keeps the historical
    #    truncate-at-first-tag behaviour exactly.
    if first_tag.start() > 0:
        head = payload[: first_tag.start()].rstrip()
        if head:
            return head

    # 2. The payload is wrapped. Prefer the first <arg_value> element's
    #    content -- in the pair shape (<arg_key>name</arg_key>
    #    <arg_value>content</arg_value>) the value is the payload and the
    #    key is just the parameter name.
    value_match = None
    key_match = None
    for m in _ARG_ELEMENT_RE.finditer(payload):
        if m.group(1).lower() == "value":
            if value_match is None:
                value_match = m
        elif key_match is None:
            key_match = m

    if value_match is not None:
        content = value_match.group(2).strip()
        if content:
            return content
    if key_match is not None:
        content = key_match.group(2).strip()
        if content:
            return content

    # 3. Wrapped but the inner content was empty -- e.g.
    #    "RUN: <arg_value></arg_value>". Nothing to act on.
    if _ARG_EMPTY_ELEMENT_RE.search(payload):
        return ""

    # 4. An opening tag with no matching close. The content after the
    #    opening tag is the best available payload.
    first_open = _ARG_OPEN_RE.search(payload)
    if first_open:
        return payload[first_open.end() :].strip()

    # 5. A stray closing tag with nothing around it. Nothing to act on.
    return ""
