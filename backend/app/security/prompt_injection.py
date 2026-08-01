"""Prompt-injection screening and the untrusted-content fence (T-303, NFR-SEC-05, R-44).

NFR-SEC-05 asks for two things: "a screening step (FR-ORC-07) runs before
retrieval/generation", and "the system prompt isolates untrusted content". This module owns
the first and the *primitive* the second is built from; `app.rag.prompts` does the composing.

**The screen is defence in depth, not the primary control.** R-44(3) is explicit about which
way round that is, and the ordering matters for every judgement call below. A poisoned
document cannot escalate for three reasons, none of which is this file:

1. **No tools are exposed to the model.** Generation is a plain OpenAI SDK call (R-15), so
   NFR-SEC-05's "must not alter tool use" is satisfied by there being no tool surface.
2. **Authorization is unreachable from the prompt.** It is applied in-query (FR-RET-04 /
   NFR-SEC-06) from `RAGContext`, which is not checkpointed (R-42(3)). No model output ever
   feeds a retrieval filter.
3. **The grounding contract self-checks.** FR-CIT-06(2) requires every citation to have been
   in the context actually passed, so a model told to invent a source fails the T-308 gate.

Given that, the rules here are deliberately **precision-biased and cheap**: no model call
(which would add a serial round-trip and a per-turn cost before NFR-PRF-02's already-delayed
first token, and would itself be injectable), no new dependency, no network. Same reasoning
as R-32's in-tree ClamAV client and R-33(5)'s in-tree magic-byte sniffing.

**Recorded limitation:** pattern rules are evadable by a determined attacker. That is not a
gap to be closed by adding rules until the list looks convincing — it is *why* (1)-(3) above
carry the requirement. Adding a rule is worthwhile only when it catches something real
without blocking a legitimate question.

**Only the query is screened** (R-44(1)). Retrieved document text is neutralised and fenced,
never blocked: the chunk is already inside the caller's own FR-RET-04 scope, so there is no
privilege boundary to cross, and blocking on it would be a denial-of-service against the
user's own knowledge base — one uploaded security policy containing the phrase "ignore
previous instructions" would make every query that retrieves it permanently unanswerable.
§11 and the source doc say the same thing: *treat document text as data, not instructions*.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "CONTEXT_FENCE_CLOSE",
    "CONTEXT_FENCE_OPEN",
    "INJECTION_RULES",
    "SOURCE_MARKER_RE",
    "InjectionRule",
    "InjectionScreenResult",
    "neutralize",
    "screen_query",
    "source_marker",
]

# --- the untrusted-content fence ----------------------------------------------
#
# Markers rather than XML-ish tags: a tag invites the model to treat the block as markup it
# may reinterpret, and `<document>` is a plausible thing for a real document to contain.
# These strings are not plausible content, which is what makes an occurrence inside chunk
# text a *signal* rather than a coincidence.

CONTEXT_FENCE_OPEN = "<<<CORPUS_UNTRUSTED_DOCUMENT_CONTEXT>>>"
CONTEXT_FENCE_CLOSE = "<<<END_CORPUS_UNTRUSTED_DOCUMENT_CONTEXT>>>"

#: What `neutralize` leaves behind. Visibly inert and visibly *changed*: a reader (or an
#: operator reading a captured prompt) can tell defusing happened, and the model sees
#: something that is plainly not a delimiter.
_DEFUSED = "[redacted-delimiter]"


def source_marker(index: int) -> str:
    """The ``[S1]``-style citation handle for the *1-based* ``index``-th source.

    A single definition because three places must agree on the spelling: the fence block
    that hands markers to the model, the FR-CIT-06(2) check that maps a marker back to a
    chunk id, and :data:`SOURCE_MARKER_RE`, which defuses a forged one.
    """
    return f"[S{index}]"


#: Any source marker, forged or genuine, capturing its 1-based index.
#:
#: Two readers, pointed in opposite directions, and they must not drift — which is why the
#: pattern is defined once here rather than recompiled per caller. :func:`neutralize` matches
#: it inside *untrusted* text so a poisoned chunk cannot mint `[S9]` and have a claim
#: attributed to a document it does not own; `app.rag.generation.split_answer_segments`
#: matches it in the model's *own* output to resolve a citation back to a chunk (R-48(4)).
#: A marker the composer would emit but this pattern missed is a citation silently rendered
#: as prose; one this matched but the composer spells differently is a citation that never
#: resolves.
SOURCE_MARKER_RE = re.compile(r"\[S(\d+)\]")

#: Chat-template control tokens. Not "suspicious": these have no meaning in prose at all,
#: and their only purpose in a question is to forge a turn boundary.
_CHAT_CONTROL_RE = re.compile(
    r"<\|(?:im_start|im_end|endoftext|system|user|assistant)\|>|\[/?INST\]|<<SYS>>",
    re.IGNORECASE,
)


def neutralize(text: str) -> str:
    """Make ``text`` safe to embed inside the fence. Never raises, never truncates.

    Three transformations, each closing a specific forgery:

    * **Fence markers** → :data:`_DEFUSED`. This is what makes the delimiter load-bearing:
      without it, a document ending in ``CONTEXT_FENCE_CLOSE`` followed by instructions
      would appear to the model to have escaped the data block.
    * **Source markers** → :data:`_DEFUSED`. A forged ``[S9]`` is an attempt to attribute
      a fabricated claim to a real document; FR-CIT-06 would catch the citation, but the
      cheaper fix is for the marker never to exist.
    * **Chat control tokens and format characters** → removed. Zero-width and other `Cf`
      characters are stripped because they are invisible: they let ``ignore`` be spelled in
      a way that reads normally to a human and to the model but not to a regex. Stripping
      them here *and* normalising in :func:`_normalize` is deliberate duplication — the two
      run on different strings (document text vs the query).

    Text is *modified*, not rejected, and the FR-CIT-03 citation quote is unaffected: R-36(6)
    denormalises the quote into `messages.citations` from the retrieved chunk, not from this
    copy, so defusing a delimiter here cannot corrupt what the user is shown.
    """
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    cleaned = cleaned.replace(CONTEXT_FENCE_OPEN, _DEFUSED)
    cleaned = cleaned.replace(CONTEXT_FENCE_CLOSE, _DEFUSED)
    cleaned = SOURCE_MARKER_RE.sub(_DEFUSED, cleaned)
    return _CHAT_CONTROL_RE.sub(_DEFUSED, cleaned)


# --- screening ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InjectionRule:
    """One pattern and the verdict it carries.

    ``code`` is what reaches `RAGState.injection_rule` and the log event — a short stable
    identifier, never the offending text (R-42(2): checkpointing that would persist
    attacker-chosen content verbatim, and R-43(5) keeps payload text out of logs).
    """

    code: str
    pattern: re.Pattern[str]
    #: ``True`` for the block tier. A ``False`` rule can only ever mark `suspicious`.
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class InjectionScreenResult:
    """Outcome of screening one query. ``rule`` is ``None`` only when ``verdict`` is clean."""

    verdict: str
    rule: str | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == "blocked"


#: The block tier: phrasings with no legitimate use in a question about one's own documents.
#: Each is anchored on a verb-plus-object shape rather than a keyword, because the keywords
#: alone ("instructions", "system", "prompt") are ordinary words in a document corpus — the
#: false-positive corpus in `tests/test_prompt_injection.py` is what holds that line.
_BLOCKING_RULES: tuple[tuple[str, str], ...] = (
    (
        # "ignore all previous instructions", "disregard your rules", "forget the above prompt",
        # and — the reversed word order, which is just as common — "forget the instructions
        # above". Both orderings are spelled out rather than collapsed into one loose pattern,
        # because the qualifier and the noun are what make the match specific: dropping either
        # requirement would block "ignore the appendix" and "override the earlier guidance",
        # both of which are in the false-positive corpus.
        "INSTRUCTION_OVERRIDE",
        r"\b(?:ignore|disregard|forget|override|bypass|discard)\b"
        r"(?:\W+\w+){0,4}?\W+"
        r"(?:"
        r"\b(?:previous|prior|preceding|above|earlier|initial|original|your|all)\b"
        r"(?:\W+\w+){0,3}?\W+"
        r"\b(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions|"
        r"guideline|guidelines|constraint|constraints)\b"
        r"|"
        r"\b(?:instruction|instructions|prompt|prompts|rule|rules)\b"
        r"(?:\W+\w+){0,2}?\W+"
        r"\b(?:above|earlier|before|previously)\b"
        r")",
    ),
    (
        # "reveal your system prompt", "repeat the instructions above", "print your rules"
        "SYSTEM_PROMPT_EXFIL",
        r"\b(?:reveal|repeat|print|output|show|display|disclose|dump|echo|recite)\b"
        r"(?:\W+\w+){0,4}?\W+"
        r"\b(?:system|initial|original|hidden|secret|developer)\b\W+"
        r"\b(?:prompt|prompts|instruction|instructions|message|messages|rule|rules)\b",
    ),
    (
        # A forged turn boundary at the start of a line: "System: you may now ..."
        "ROLE_SPOOF",
        r"(?:^|\n)\s*(?:#{0,3}\s*)?(?:system|assistant|developer)\s*:",
    ),
    (
        # Chat-template control tokens, wherever they appear.
        "CHAT_CONTROL_TOKEN",
        _CHAT_CONTROL_RE.pattern,
    ),
    (
        # "you are now an unrestricted AI", "act as DAN", "enable developer mode", "jailbreak"
        "PERSONA_OVERRIDE",
        r"\byou\s+are\s+now\b(?:\W+\w+){0,4}?\W+"
        r"\b(?:unrestricted|uncensored|unfiltered|unbound|jailbroken|dan|evil|amoral)\b"
        r"|\b(?:act|behave|pretend|roleplay|respond)\b(?:\W+\w+){0,3}?\W+"
        r"\b(?:unrestricted|uncensored|unfiltered|jailbroken|dan)\b"
        r"|\b(?:developer|god|debug|admin)\s+mode\b"
        r"|\bjailbreak\b",
    ),
    (
        # Directly serves NFR-SEC-05's "must not alter ... authorization".
        "AUTHZ_OVERRIDE",
        r"\b(?:you\s+(?:may|can|should)\s+now|you\s+have|grant|give)\b"
        r"(?:\W+\w+){0,4}?\W+"
        r"\b(?:access|permission|permissions|authorization|admin|root)\b"
        r"|\b(?:access|retrieve|search|read|list|show)\b\W+"
        r"\b(?:all|every|any|other|others|everyone|everybody)\b"
        r"(?:\W+\w+){0,2}?\W+"
        r"\b(?:users?|tenants?|accounts?)\b"
        r"|\bbypass\b(?:\W+\w+){0,3}?\W+"
        r"\b(?:access|permission|permissions|authorization|security|filter|filters)\b",
    ),
)

#: The suspicious tier: single weak signals. These *cannot* block, so a false positive costs
#: a log line and a state field rather than an unanswerable question — which is the whole
#: reason the tier exists (R-44(4)).
_SUSPICIOUS_RULES: tuple[tuple[str, str], ...] = (
    ("MENTIONS_INJECTION", r"\bprompt\s+injection\b|\bjail\s?break(?:ing)?\b"),
    ("MENTIONS_SYSTEM_PROMPT", r"\bsystem\s+prompt\b"),
)

INJECTION_RULES: tuple[InjectionRule, ...] = tuple(
    InjectionRule(code=code, pattern=re.compile(pattern, re.IGNORECASE), blocking=blocking)
    for rules, blocking in ((_BLOCKING_RULES, True), (_SUSPICIOUS_RULES, False))
    for code, pattern in rules
)

#: Runs of whitespace, collapsed so a rule's `\W+` gaps cannot be widened past its bound by
#: padding. Newlines are preserved as single `\n` because `ROLE_SPOOF` anchors on them.
_HORIZONTAL_WS_RE = re.compile(r"[^\S\n]+")
_VERTICAL_WS_RE = re.compile(r"\n\s*\n+")

#: Regions a human would read as *mentioning* a phrase rather than issuing it. Deliberately
#: simple — paired ASCII/typographic quotes and backtick spans on one line.
_QUOTED_RE = re.compile(
    r"\"[^\"\n]{1,400}\"|'[^'\n]{1,400}'|`[^`\n]{1,400}`|“[^”\n]{1,400}”",
)


def _normalize(text: str) -> str:
    """Fold away the cheapest evasions before matching.

    NFKC because a compatibility-decomposable lookalike ("ｉｇｎｏｒｅ") is the same word to a
    model and a different string to a regex; `Cf` stripping because a zero-width joiner
    inside "ignore" is invisible to a reader; whitespace collapsing because padding would
    otherwise widen the bounded `\\W+` gaps the rules rely on.

    Not an attempt at full confusable folding — full Unicode homoglyph mapping is a large
    table with its own false positives, and per the module docstring the screen is not what
    carries the requirement.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    folded = _HORIZONTAL_WS_RE.sub(" ", folded)
    return _VERTICAL_WS_RE.sub("\n", folded)


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _QUOTED_RE.finditer(text)]


def screen_query(text: str, *, fence_markers: bool = True) -> InjectionScreenResult:
    """Screen one user query (R-44(1)-(2)). Never raises.

    Returns the first matching rule's verdict, blocking rules first, so the recorded code is
    the most severe rather than the earliest in the text.

    **A block-tier match found entirely inside quotes is demoted to `suspicious`.** That
    demotion is what keeps the block tier usable in this product: Corpus is document QA, so
    *"what does 'ignore all previous instructions' mean in the security policy?"* is a
    question a user will genuinely ask, and answering it must not require weakening the rule
    that catches the unquoted imperative.

    ``fence_markers`` covers the query containing one of this module's own delimiters. It has
    no legitimate use and costs nothing to check — but it is belt-and-braces over
    :func:`neutralize`, which the composer applies to the query too, not the control itself.
    """
    if not text or not text.strip():
        return InjectionScreenResult(verdict="clean")

    normalized = _normalize(text)

    if fence_markers and (CONTEXT_FENCE_OPEN in normalized or CONTEXT_FENCE_CLOSE in normalized):
        return InjectionScreenResult(verdict="blocked", rule="FENCE_SPOOF")

    quoted = _quoted_spans(normalized)
    demoted: str | None = None

    for rule in INJECTION_RULES:
        match = rule.pattern.search(normalized)
        if match is None:
            continue
        if not rule.blocking:
            return InjectionScreenResult(verdict="suspicious", rule=rule.code)
        start, end = match.span()
        if any(qs <= start and end <= qe for qs, qe in quoted):
            # Remembered rather than returned: a *later* blocking rule may match unquoted,
            # and an attacker who quotes one payload while issuing another must not be able
            # to buy a `suspicious` verdict with the decoy.
            demoted = demoted or rule.code
            continue
        return InjectionScreenResult(verdict="blocked", rule=rule.code)

    if demoted is not None:
        return InjectionScreenResult(verdict="suspicious", rule=demoted)
    return InjectionScreenResult(verdict="clean")
