"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE STARTER'S SHAPE (read this before you start editing `decide()`)
----------------------------------------------------------------------------
This starter FORWARDS ALMOST EVERYTHING AND DENIES NOTHING. That is not a
placeholder oversight — it is the honest zero-defence baseline you are
meant to beat: `bots/rookie` in the kit's own ladder does exactly the same
thing, and RULES.md's own words are "if you cannot beat Rookie you have a
bug, not a strategy." `decide()` below is structured as four named jobs —
ROUTE, ADMIT, AUTHORIZE, BUDGET — each with a one-line TODO naming what a
real implementation checks and why. None of the four currently rejects,
rewrites, or reroutes anything; they are seams, not solutions. Fill them in
using `agent/strategy.py` (routing/budget policy) and `agent/guardrails.py`
(the safety checks) — both already import cleanly from here.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

# Import strategy building blocks — degrade gracefully per workspace hard rule 2.
try:
    from agent.strategy import (
        BudgetPacer,
        ResultCache,
        is_catalog_trap,
        successor_of,
        pick_replica,
        cheap_mask,
    )
    _STRATEGY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _STRATEGY_AVAILABLE = False

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

# Tools that change state — same set used by bots/operator, referee/detectors.
WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset({
    ("progress", "record_mastery"),
    ("content", "flag_stale_slide"),
})

# Known A2A peer servers.
A2A_SERVERS: frozenset[str] = frozenset({
    "curriculum-analyst", "citation-checker", "roster",
})

# Narrow, cheap field masks per tool — copied from operator (the good part).
# These are the fields an answer actually cites; requesting more is wasteful.
DEFAULT_MASKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("anchor", "title"),
    ("slides", "get_frame"): ("anchor", "body", "title"),
    ("glossary", "define"): ("anchor", "definition"),
    ("registry", "provenance"): ("etag", "replica"),
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("anchor", "term"),
    ("research", "search"): ("anchor", "url"),
}

# Injection-pattern keywords to detect in command arguments. Used by ADMIT
# to quarantine commands carrying obviously injected instructions. Calibrated
# to be less trigger-happy than operator's list to avoid blank false positives.
_INJECTION_KEYWORDS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "system override",
    "as the system, you must",
    "reveal the act",
    "reveal all learner",
    "print the act",
)


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes persist across the entire duel. `agent/strategy.py`
    provides BudgetPacer, ResultCache, and replica-choice heuristics that
    are wired into the four JOBs below.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory ------------------------------------------------
        self._seen_anchors: dict[str, Any] = {}
        self._credits_authorised: int = 0
        self._denied_cmd_ids: set[str] = set()

        # Budget pacer from strategy.py — tracks spend across the duel.
        self._pacer: BudgetPacer | None = (
            BudgetPacer() if _STRATEGY_AVAILABLE else None
        )

        # Track failed commands to avoid wasteful identical retries.
        self._failed_calls: set[tuple[str, str, str]] = set()  # (server, tool, args_key)

    # ------------------------------------------------------------------
    # PRIVATE HELPERS
    # ------------------------------------------------------------------

    def _is_write(self, cmd: Command) -> bool:
        """True iff `(cmd.server, cmd.tool)` is a state-changing write tool."""
        return (cmd.server, cmd.tool) in WRITE_TOOLS

    def _extract_target_learner(self, cmd: Command) -> str | None:
        """Extract the target learner from a command's args, if any.
        Write tools like `record_mastery` carry a `learner` or `learner_id`
        arg naming whose record is being written to."""
        return (
            cmd.args.get("learner")
            or cmd.args.get("learner_id")
            or cmd.args.get("target_learner")
        )

    def _args_key(self, cmd: Command) -> str:
        """A rough fingerprint of a command's identity for retry detection."""
        parts = sorted(f"{k}={v}" for k, v in cmd.args.items())
        return f"{cmd.server}.{cmd.tool}({','.join(parts)})"

    def _looks_injected(self, cmd: Command) -> bool:
        """True iff the command's args contain injection-pattern keywords.
        Calibrated to be conservative to avoid blank false positives."""
        blob = " ".join(str(v) for v in cmd.args.values()).lower()
        return any(kw in blob for kw in _INJECTION_KEYWORDS)

    def _effective_fields(self, cmd: Command) -> tuple[str, ...]:
        """Return the fields to actually request. If the command's fields
        are empty or ("*",) for a known tool, narrow them to the cheap
        default mask from DEFAULT_MASKS."""
        key = (cmd.server, cmd.tool)
        fields = cmd.fields

        # Rewrite catalog trap tools to narrow masks.
        if _STRATEGY_AVAILABLE and is_catalog_trap(cmd.server, cmd.tool, fields):
            narrow = DEFAULT_MASKS.get(key)
            if narrow:
                self._telemetry.event(
                    "gateway.mask_rewrite",
                    server=cmd.server, tool=cmd.tool,
                    original=fields, rewritten=narrow,
                    reason="catalog_trap",
                )
                return narrow

        # If no fields specified and we have a default mask, use it.
        if not fields and key in DEFAULT_MASKS:
            return DEFAULT_MASKS[key]

        return fields

    # ------------------------------------------------------------------
    # THE FOUR JOBS
    # ------------------------------------------------------------------

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).

        Four jobs, executed in order: ROUTE, ADMIT, AUTHORIZE, BUDGET.
        Each can deny or rewrite the command. If all pass, the command
        is forwarded with its (possibly rewritten) fields and headers."""
        self._telemetry.decision_seen(cmd)

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: deprecated tool rewrite + replica awareness.
        #
        # Replace deprecated tools with their successors (free win: avoids
        # the `wasteful` detector for deprecated tool usage). Replica
        # routing is handled by pick_replica when drift is suspected.
        # ------------------------------------------------------------------
        routed_server = cmd.server
        routed_tool = cmd.tool

        if _STRATEGY_AVAILABLE:
            succ = successor_of(cmd.server, cmd.tool)
            if succ:
                routed_server, routed_tool = succ
                self._telemetry.event(
                    "gateway.route_rewrite",
                    original=f"{cmd.server}.{cmd.tool}",
                    rewritten=f"{routed_server}.{routed_tool}",
                    reason="deprecated_successor",
                )

        # ------------------------------------------------------------------
        # JOB 2 — ADMIT: deny calls that are doomed to fail.
        #
        # A deny costs ZERO credits — a deny you can defend beats a forward
        # you can't.
        # ------------------------------------------------------------------

        # 2a. get_frame without a lease is protocol_misuse.
        if routed_tool == "get_frame" and not cmd.lease_id:
            if not self.ctx.leases:
                return self.deny(cmd, "no live lease for get_frame — protocol_misuse risk")

        # 2b. Quarantine commands with obvious injection patterns in args.
        if self._looks_injected(cmd):
            self._telemetry.event(
                "gateway.quarantine",
                cmd_id=cmd.cmd_id, server=cmd.server, tool=cmd.tool,
                reason="injection_pattern_in_args",
            )
            return self.deny(cmd, "quarantine: injection-pattern content detected in arguments")

        # ------------------------------------------------------------------
        # JOB 3 — AUTHORIZE: does this command belong to WHOM YOU SERVE?
        #
        # This is the Day 26 thesis: authority derives from `ctx.act`
        # (whom you serve), NOT from `ctx.sub` (what you are). A write
        # targeting a different learner is `authority_exceeded` (weight 10).
        # ------------------------------------------------------------------

        # 3a. Write authorization: target learner must match ctx.act.
        if self._is_write(cmd):
            target = self._extract_target_learner(cmd)
            if target and target != self.ctx.act:
                self._telemetry.event(
                    "gateway.authority_denied",
                    cmd_id=cmd.cmd_id,
                    target=target,
                    act=self.ctx.act,
                )
                return self.deny(
                    cmd,
                    f"authority_exceeded: write targets {target} but act is {self.ctx.act}",
                )

        # 3b. A2A delegation: verify the command is for a known peer.
        # (The arena's own card verification handles cryptographic checks;
        # we check that the peer is in the known set.)
        if cmd.kind == "a2a" and cmd.server not in A2A_SERVERS:
            self._telemetry.event(
                "gateway.unknown_peer",
                cmd_id=cmd.cmd_id, server=cmd.server,
            )
            return self.deny(cmd, f"unknown A2A peer: {cmd.server}")

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: can the duel afford this call?
        #
        # Narrow expensive masks, deny when credits are critically low.
        # ------------------------------------------------------------------

        effective_fields = self._effective_fields(cmd)

        # 4a. If credits are very low (< 5), deny non-essential calls.
        if self.ctx.credits < 5 and not self._is_write(cmd):
            return self.deny(cmd, f"budget_exhausted: only {self.ctx.credits} credits remain")

        # Build the final ToolCall with potentially rewritten fields/server/tool.
        call = self._build_tool_call(
            cmd,
            server=routed_server,
            tool=routed_tool,
            fields=effective_fields,
        )

        # Determine verdict: "rewrite" if anything was changed, else "forward".
        changed = (
            routed_server != cmd.server
            or routed_tool != cmd.tool
            or effective_fields != cmd.fields
        )
        verdict = "rewrite" if changed else "forward"

        decision = Decision(verdict=verdict, call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Deny a command. Costs ZERO credits (CONTRACTS.md 4.1)."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _build_tool_call(
        self,
        cmd: Command,
        *,
        server: str | None = None,
        tool: str | None = None,
        fields: tuple[str, ...] | None = None,
    ) -> "ToolCall":
        """`Command` -> the `ToolCall` the arena will execute.
        Allows overriding server/tool/fields for rewrite decisions."""
        tc_fields = {
            "server": server or cmd.server,
            "tool": tool or cmd.tool,
            "args": dict(cmd.args),
            "fields": fields if fields is not None else cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**tc_fields)
        return tc_fields  # type: ignore[return-value]

    # Keep the old name as an alias for backward compatibility with __main__ demo.
    _to_tool_call = _build_tool_call


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — the naive starter forwards everything ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict == "forward"
        assert decision.call is not None
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        assert call_dict["server"] == cmd.server
        assert call_dict["tool"] == cmd.tool
        assert tuple(call_dict["fields"]) == cmd.fields

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
