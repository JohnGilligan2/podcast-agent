"""Per-client source policy: what counts as evidence for one agency client.

Each client agrees three lists with the agency at onboarding (data/clients/<client>.json):

  trusted_domains  sources the client treats as authoritative, ideally by topic
                   (fda.gov for medical, nasa.gov for space ...)
  blocked_domains  sites that must never be cited, e.g. because they clash with brand values
  pinned_facts     client-supplied facts that are not on the public web (own product claims)

Every piece of evidence gets a tier, and the tier drives confidence and human review:

  pinned       client-supplied fact                         trusted
  reference    curated local KB (offline / test mode)       trusted
  trusted_web  live web result on a trusted domain          trusted
  other_web    live web result on any other domain          confidence capped, flagged for review
  (blocked)    never enters the evidence whitelist at all

The agent never edits this file. A transcript is untrusted input, so source lists change only
through an admin path outside the processing run (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .knowledge_base import KBDocument

TIER_PINNED, TIER_REFERENCE, TIER_TRUSTED_WEB, TIER_OTHER_WEB = "pinned", "reference", "trusted_web", "other_web"
TRUSTED_TIERS = frozenset({TIER_PINNED, TIER_REFERENCE, TIER_TRUSTED_WEB})
SEARCH_MODES = ("prefer_trusted", "trusted_only")
UNTRUSTED_MAX_CONFIDENCE = 0.6


def domain_of(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    return host.lower().removeprefix("www.")


def _on_domain(host: str, domain: str) -> bool:
    domain = domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class TrustedDomain:
    domain: str
    topics: tuple[str, ...] = ()


@dataclass
class ClientSources:
    client_id: str = "none"
    name: str = "No client policy"
    search_mode: str = "prefer_trusted"
    trusted_domains: list[TrustedDomain] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    pinned_facts: list[KBDocument] = field(default_factory=list)
    version: str = "none"   # content hash, recorded in every report

    @classmethod
    def load(cls, path: str | Path) -> "ClientSources":
        raw = Path(path).read_bytes()
        data = json.loads(raw)
        mode = data.get("search_mode", "prefer_trusted")
        if mode not in SEARCH_MODES:
            raise ValueError(f"{path}: search_mode must be one of {SEARCH_MODES}, got {mode!r}")
        trusted = [TrustedDomain(d["domain"], tuple(d.get("topics", []))) if isinstance(d, dict)
                   else TrustedDomain(d) for d in data.get("trusted_domains", [])]
        blocked = [d["domain"] if isinstance(d, dict) else d for d in data.get("blocked_domains", [])]
        overlap = {t.domain for t in trusted} & set(blocked)
        if overlap:
            raise ValueError(f"{path}: domains are both trusted and blocked: {sorted(overlap)}")
        if mode == "trusted_only" and not trusted:
            raise ValueError(f"{path}: search_mode 'trusted_only' needs at least one trusted domain")
        pinned = [KBDocument(id=f["id"], title=f["title"], source=f.get("source", data.get("name", "client")),
                             url=f.get("url"), as_of=f.get("as_of"), tags=f.get("tags", []),
                             content=f["content"], tier=TIER_PINNED)
                  for f in data.get("pinned_facts", [])]
        return cls(client_id=data["client_id"], name=data.get("name", data["client_id"]), search_mode=mode,
                   trusted_domains=trusted, blocked_domains=blocked, pinned_facts=pinned,
                   version=hashlib.sha256(raw).hexdigest()[:12])

    # ---- classification -----------------------------------------------------------------

    def is_blocked(self, url: str) -> bool:
        host = domain_of(url)
        return any(_on_domain(host, d) for d in self.blocked_domains)

    def is_trusted(self, url: str) -> bool:
        host = domain_of(url)
        return any(_on_domain(host, t.domain) for t in self.trusted_domains)

    def web_tier(self, url: str) -> str:
        return TIER_TRUSTED_WEB if self.is_trusted(url) else TIER_OTHER_WEB

    # ---- what the model sees ------------------------------------------------------------

    def web_search_tool(self, max_uses: int) -> dict:
        """Anthropic's server-side web search, steered by this policy. The API accepts
        allowed_domains OR blocked_domains, never both, so 'trusted_only' sends the allow list
        and 'prefer_trusted' sends the block list. Blocked domains are also filtered in code."""
        tool: dict = {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}
        if self.search_mode == "trusted_only":
            tool["allowed_domains"] = [t.domain for t in self.trusted_domains]
        elif self.blocked_domains:
            tool["blocked_domains"] = list(self.blocked_domains)
        return tool

    def describe_for_prompt(self) -> str:
        if not (self.trusted_domains or self.blocked_domains or self.pinned_facts):
            return "No client source policy is configured."
        by_topic: dict[str, list[str]] = {}
        for t in self.trusted_domains:
            for topic in t.topics or ("general",):
                by_topic.setdefault(topic, []).append(t.domain)
        trusted = "; ".join(f"{topic}: {', '.join(ds)}" for topic, ds in by_topic.items()) or "none"
        mode = ("web search is restricted to the trusted domains" if self.search_mode == "trusted_only"
                else "web search covers the open web minus blocked domains; prefer trusted domains")
        return (f"Client: {self.name} (source policy {self.version}; {mode}).\n"
                f"Trusted domains by topic — {trusted}.\n"
                f"Blocked domains (never cite): {', '.join(self.blocked_domains) or 'none'}.\n"
                f"Pinned client facts: {len(self.pinned_facts)} (returned by search_knowledge_base "
                f"with tier 'pinned').")
