"""MCP server: an operator console for People Ops and IT, usable in Claude Desktop, Claude Code
or n8n's AI Agent node.

Most tools are read-only. resolve_exception is the one write tool, and it goes through the
same engine path as a Slack click. The audit trail records it as `mcp:<operator>`.
Transport is stdio for local use. Over HTTP in production it would sit behind Okta OAuth
(see docs/03_build_note.md), with resolve_exception requiring a write scope.

Supports mcp>=2 (MCPServer) and mcp 1.x (FastMCP).
"""
from __future__ import annotations

from typing import Callable

from .service import Service

INSTRUCTIONS = (
    "Operator console for BetterUp's onboarding sync engine (Ashby, Workday, Okta/Lumos, ExpoIT, cohort "
    "tracker). Use list_hires / get_hire / diff_hire / list_exceptions to investigate. resolve_exception "
    "changes real systems: always confirm the option with the human before calling it.")


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer      # mcp >= 2
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP          # mcp 1.x
        return FastMCP


def _annotations(read_only: bool):
    try:
        from mcp.types import ToolAnnotations
        return {"annotations": ToolAnnotations(readOnlyHint=read_only, destructiveHint=not read_only)}
    except Exception:
        return {}


def build_server(svc_factory: Callable[[], Service] = Service.from_env):
    server = _server_class()(name="onboard-sync", instructions=INSTRUCTIONS)
    holder: dict[str, Service] = {}

    def svc() -> Service:
        if "svc" not in holder:
            holder["svc"] = svc_factory()
        return holder["svc"]

    def resolve_hire(hire: str) -> str:
        hid = svc().find_hire(hire)
        if not hid:
            raise ValueError(f"no hire matches '{hire}'")
        return hid

    @server.tool(**_annotations(True))
    def list_hires() -> dict:
        """List in-flight hires with lifecycle, sync state, start date, connected systems and open exceptions."""
        return {"hires": svc().hires()}

    @server.tool(**_annotations(True))
    def get_hire(hire: str) -> dict:
        """Full view of one hire (id or name): canonical record, field provenance, gates, exceptions, events."""
        return svc().hire(resolve_hire(hire))

    @server.tool(**_annotations(True))
    def diff_hire(hire: str) -> dict:
        """Dry run: validation findings and what the engine would change in each system right now."""
        return svc().diff(resolve_hire(hire))

    @server.tool(**_annotations(True))
    def list_exceptions(include_closed: bool = False) -> dict:
        """Open exceptions (or all) with Claude's recommendation and the allowed options."""
        return {"exceptions": svc().exceptions(include_closed)}

    @server.tool(**_annotations(True))
    def explain_exception(exception_id: str) -> dict:
        """One exception with its facts, recommendation, options and the hire's recent audit trail."""
        ex = svc().exception_view(exception_id)
        return {**ex, "recent_audit": svc().audit(ex["hire_id"])[-8:]}

    @server.tool(**_annotations(False))
    def resolve_exception(exception_id: str, option: str, operator: str) -> dict:
        """Resolve an exception with one of its listed options. Writes to real systems; confirm first.
        `operator` is the human's identifier and is recorded in the audit trail."""
        return svc().resolve(exception_id, option, actor=f"mcp:{operator}")

    @server.tool(**_annotations(True))
    def audit_trail(hire: str) -> dict:
        """Hash-chained audit entries for one hire (who/what changed which system, and why)."""
        return {"entries": svc().audit(resolve_hire(hire)), "chain": svc().verify_audit()}

    @server.tool(**_annotations(False))
    def run_sweep() -> dict:
        """Run the anti-entropy sweep now (drift detection, retries, gate reminders)."""
        report = svc().sweep()
        return {"started": report["started"],
                "reconciled": {h: r.get("sync_state") for h, r in report["reconciled"].items()}}

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
