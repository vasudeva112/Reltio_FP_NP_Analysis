"""
Match token classes and token-phrase generation (candidate selection only).

Reltio: tokens of the attributes combined by AND are concatenated into token
phrases; profiles sharing a phrase become match candidates; phrases shared by
more than 300 profiles are over-collided and skipped. exactOrNull / exactOrAllNull
and constraints produce no tokens, ignoreInToken suppresses an attribute's tokens.
"""

import itertools
import re
from typing import Dict, List, Optional, Set

from . import comparator_registry as registry
from .cleansers import PreparedValues
from .comparators.address_phone import ADDRESS_NOISE, address_words, strip_noise
from .comparators.base import APPROXIMATION, DOCUMENTED, EngineContext
from .comparators.phonetic import double_metaphone
from .exceptions_log import LOG
from .operators import _constraint_one
from .rule_config_parser import CONSTRAINT_OPS, MatchGroup, Node, short_attr, short_class

TOKEN_REGISTRY: Dict[str, type] = {}
NO_TOKENS = None          # a node that does not take part in tokenisation


def register_token(cls):
    TOKEN_REGISTRY[cls.RELTIO_NAME] = cls
    return cls


class TokenClass:
    RELTIO_NAME = ""
    STATUS = APPROXIMATION
    DOC = ""

    def __init__(self, params: Optional[dict], ctx: EngineContext, group: str = "", attribute: str = ""):
        self.params = params or {}
        self.ctx = ctx
        self.group = group
        self.attribute = attribute

    def tokens(self, value: str) -> Set[str]:  # pragma: no cover
        raise NotImplementedError


@register_token
class ExactMatchToken(TokenClass):
    RELTIO_NAME = "ExactMatchToken"
    STATUS = DOCUMENTED
    DOC = "One token: the lower-cased value."

    def tokens(self, value: str) -> Set[str]:
        v = re.sub(r"\s+", " ", value.strip().lower())
        return {v} if v else set()


@register_token
class DoubleMetaphoneMatchToken(TokenClass):
    RELTIO_NAME = "DoubleMetaphoneMatchToken"
    STATUS = DOCUMENTED
    DOC = "Primary and secondary Double Metaphone codes (each a token)."

    def tokens(self, value: str) -> Set[str]:
        codes = double_metaphone(value.strip().lower())
        return set(codes) if codes else ExactMatchToken(None, self.ctx).tokens(value)


def _words(value: str) -> List[str]:
    return re.findall(r"\w+", value.lower())


class _NoiseAware(TokenClass):
    def _noise(self) -> set:
        nd = self.params.get("noiseDictionary")
        if not nd:
            return set()
        d = self.ctx.noise_dictionaries.get(nd)
        if d is None:
            LOG.add("APPROXIMATION", f"Noise dictionary not available locally: {nd}",
                    group=self.group, attribute=short_attr(self.attribute))
            return set()
        return d


@register_token
class DistinctWordsMatchToken(_NoiseAware):
    RELTIO_NAME = "DistinctWordsMatchToken"
    STATUS = APPROXIMATION
    DOC = "Each distinct word minus noise words. threshold/useStemmer/useSoundex not reproduced."

    def tokens(self, value: str) -> Set[str]:
        noise = self._noise()
        words = [w for w in _words(value) if w not in noise] or _words(value)
        return set(words)


@register_token
class FuzzyTextMatchToken(DistinctWordsMatchToken):
    RELTIO_NAME = "FuzzyTextMatchToken"
    DOC = "Approximation: distinct words (Reltio algorithm not documented)."


@register_token
class OrganizationNameMatchToken(DistinctWordsMatchToken):
    RELTIO_NAME = "OrganizationNameMatchToken"
    DOC = "Approximation: distinct words minus noise (Reltio organisation-name logic not documented)."


@register_token
class AddressLineMatchToken(TokenClass):
    RELTIO_NAME = "AddressLineMatchToken"
    STATUS = APPROXIMATION
    DOC = "Approximation: street-name words (non-numeric, non-noise) sorted and joined."

    def tokens(self, value: str) -> Set[str]:
        words = strip_noise(address_words(value), ADDRESS_NOISE)
        name = sorted(w for w in words if not w.isdigit()) or words
        return {" ".join(name)} if name else set()


@register_token
class PhoneNumberMatchToken(TokenClass):
    RELTIO_NAME = "PhoneNumberMatchToken"
    STATUS = APPROXIMATION
    DOC = "Right-most 10 digits (mirrors PhoneNumberComparator)."

    def tokens(self, value: str) -> Set[str]:
        d = re.sub(r"\D", "", value)
        return {d[-10:]} if len(d) >= 10 else set()


@register_token
class CustomMatchToken(TokenClass):
    RELTIO_NAME = "CustomMatchToken"
    STATUS = APPROXIMATION
    DOC = "Container of token-class groups; tokens of several groups are concatenated."

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.subs = []
        for g in self.params.get("groups") or []:
            cp = dict(g.get("classParams") or {})
            for opt in ("useStemmer", "useSoundex", "threshold"):
                if opt in cp:
                    LOG.add("APPROXIMATION", f"CustomMatchToken classParam '{opt}' not reproduced",
                            group=self.group, attribute=short_attr(self.attribute), severity="INFO")
            p = dict(cp)
            if g.get("noiseDictionary"):
                p["noiseDictionary"] = g["noiseDictionary"]
            sub = make_token_class(g.get("className", ""), p, self.ctx, self.group, self.attribute)
            pattern = re.compile(g["pattern"]) if g.get("pattern") else None
            self.subs.append((sub, pattern))
        if not self.subs:
            self.subs.append((ExactMatchToken(None, self.ctx), None))

    def tokens(self, value: str) -> Set[str]:
        parts = []
        for sub, pattern in self.subs:
            v = value
            if pattern is not None:
                m = pattern.search(value)
                if not m:
                    return set()
                v = m.group(1) if m.groups() else m.group(0)
            t = sub.tokens(v)
            if not t:
                return set()
            parts.append(sorted(t))
        return {":".join(p) for p in itertools.product(*parts)}


@register_token
class CrossMultiToken(TokenClass):
    RELTIO_NAME = "CrossMultiToken"
    STATUS = APPROXIMATION
    DOC = "Approximation: member-attribute tokens combined order-insensitively."

    def tokens(self, value: str) -> Set[str]:
        return ExactMatchToken(None, self.ctx).tokens(value)


def make_token_class(class_name: str, params: Optional[dict], ctx: EngineContext,
                     group: str = "", attribute: str = "") -> TokenClass:
    name = short_class(class_name) or "ExactMatchToken"
    cls = TOKEN_REGISTRY.get(name)
    if cls is None:
        LOG.add("UNSUPPORTED_TOKEN_CLASS", f"Match token class {name} not implemented - ExactMatchToken used",
                group=group, attribute=short_attr(attribute), severity="ERROR")
        cls = ExactMatchToken
    return cls(params, ctx, group, attribute)


# ----------------------------------------------------------------------
class GroupTokenizer:
    """Builds the token phrases of one entity for one match group."""

    def __init__(self, group: MatchGroup, ctx: EngineContext, max_phrases: int = 2000):
        self.g = group
        self.ctx = ctx
        self.max_phrases = max_phrases
        self._classes: Dict[str, TokenClass] = {}
        self.top_constraints = [n for n in group.root.children
                                if n.kind == "op" and n.op in CONSTRAINT_OPS
                                and str(n.params.get("strict", True)).lower() != "false"]
        for n in group.root.children:                           # one nested AND level (common layout)
            if n.kind == "and":
                self.top_constraints += [c for c in n.children if c.kind == "op" and c.op in CONSTRAINT_OPS
                                         and str(c.params.get("strict", True)).lower() != "false"]

    def token_class(self, attr: str) -> TokenClass:
        tc = self._classes.get(attr)
        if tc is None:
            m = self.g.token_classes.get(attr)
            if m is not None:
                tc = make_token_class(m.class_name, m.params, self.ctx, self.g.uri, attr)
            else:
                if self.g.group_token_class:
                    name = self.g.group_token_class
                else:
                    cm = self.g.comparators.get(attr)
                    cmp_cls = registry.registered().get(short_class(cm.class_name)) if cm else None
                    name = cmp_cls.DEFAULT_TOKEN_CLASS if cmp_cls else "com.reltio.match.token.ExactMatchToken"
                LOG.add("DEFAULT_TOKEN_CLASS", f"No matchTokenClasses mapping - {short_class(name)} assumed",
                        group=self.g.uri, attribute=short_attr(attr), severity="INFO")
                tc = make_token_class(name, {}, self.ctx, self.g.uri, attr)
            self._classes[attr] = tc
        return tc

    def eligible(self, prepared: Dict[str, PreparedValues]) -> bool:
        """Entities failing a strict top-level constraint get no tokens (documented)."""
        return all(_constraint_one(n.op, n.values, prepared[n.attribute], self.ctx)
                   for n in self.top_constraints)

    def _attr_tokens(self, attr: str, prepared: Dict[str, PreparedValues]) -> Set[str]:
        tc = self.token_class(attr)
        out: Set[str] = set()
        for v in prepared[attr].values:
            out |= tc.tokens(v)
        return {f"{short_attr(attr)}={t}" for t in out}

    def _node(self, node: Node, prepared) -> Optional[Set[str]]:
        if node.kind == "not":
            return NO_TOKENS
        if node.kind == "op":
            if node.op in ("exact", "fuzzy"):
                if node.attribute in self.g.ignore_in_token:
                    return NO_TOKENS
                return self._attr_tokens(node.attribute, prepared)
            if node.op == "multi":
                if node.attribute in self.g.ignore_in_token:
                    return NO_TOKENS
                member_sets = [sorted(self._attr_tokens(a, prepared)) for a in node.attributes]
                if any(not s for s in member_sets):
                    return set()
                stripped = [[t.split("=", 1)[1] for t in s] for s in member_sets]
                return {f"{short_attr(node.attribute)}=" + "+".join(sorted(combo))
                        for combo in itertools.product(*stripped)}
            return NO_TOKENS
        kids = [k for k in (self._node(c, prepared) for c in node.children) if k is not NO_TOKENS]
        if not kids:
            return NO_TOKENS
        if node.kind == "or":
            return set().union(*kids)
        result = {""}
        for k in kids:                                      # AND -> concatenation (product)
            if not k:
                return set()
            result = {f"{a}|{b}" if a else b for a in result for b in sorted(k)}
            if len(result) > self.max_phrases:
                LOG.add("TOKEN_EXPLOSION", f"More than {self.max_phrases} token phrases for one entity - truncated",
                        group=self.g.uri)
                result = set(sorted(result)[: self.max_phrases])
        return result

    def phrases(self, prepared: Dict[str, PreparedValues]) -> Set[str]:
        if not self.eligible(prepared):
            return set()
        res = self._node(self.g.root, prepared)
        return res or set()

    def status(self) -> str:
        sts = {tc.STATUS for tc in self._classes.values()}
        return APPROXIMATION if APPROXIMATION in sts or not sts else DOCUMENTED


TOKEN_DOCS = {name: dict(status=cls.STATUS, doc=cls.DOC) for name, cls in TOKEN_REGISTRY.items()}
