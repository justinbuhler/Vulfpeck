# 0014. Codex Smartsheet tools are read-only

Status: proposed
Date: 2026-10-22
Components: .codex/config.toml

## Context

Claude tool deny rules do not govern Codex's native Smartsheet MCP connection or its duplicate app route.
Credentials belong to the user's connection configuration, outside this repository.

## Decision

Port the audited native read allowlist and duplicate app restriction from LA28 commit `2d3f59e7a857ca3d140e699df25b42d0e133e3fe`.
Declare only the public MCP endpoint and approved tool names, and disable the duplicate Smartsheet app in this project.
Track only `.codex/config.toml`; ignore sibling and nested local state.
Keep an independent test oracle that rejects write tools, unknown future tools, credential fields and a changed endpoint.

## Consequences

New native tools require policy review before becoming available.
The project config inherits the user's credential transport without storing secrets or helper commands.
This policy restricts the MCP surface; token authority and shell access are unchanged.
