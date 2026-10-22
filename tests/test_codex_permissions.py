"""Pin the audited native Smartsheet read policy independently of its config."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PASS, FAIL = [], []


def check(name, cond, detail=""):
    """Report whether the named policy holds."""
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


# Audited service catalogue, never read from the configuration under test.
SMARTSHEET_READ_TOOLS = {
    "get_resource_guide", "get_cell_history", "find_in_sheet", "get_columns",
    "plan_change", "get_sheet_aggregates", "get_sheet_path", "get_sheet_summary",
    "get_sheet_version", "browse_folder", "browse_workspace", "list_workspaces",
    "search", "search_tools", "execute_portfolio_read", "execute_scenario_planning_read",
    "execute_automations_read", "execute_sharing_read", "execute_users_read",
    "execute_discussions_read", "execute_reports_read", "execute_attachments_read",
    "execute_dashboards_read",
}
SMARTSHEET_APP = "asdk_app_6a1ed9b99f888191aac2020e4cb301ff"


def policy_checks(config):
    """Check the public transport, approved tools and duplicate app restriction."""
    servers = config.get("mcp_servers", {})
    native = servers.get("smartsheet", {})
    allowed = set(native.get("enabled_tools", []))
    apps = config.get("apps", {})
    duplicate = apps.get(SMARTSHEET_APP, {})
    return {
        "native config declares the public Smartsheet transport": (
            native.get("url") == "https://mcp.smartsheet.com"
        ),
        "all audited Smartsheet reads remain available": SMARTSHEET_READ_TOOLS <= allowed,
        "writes and unknown future tools remain unavailable": allowed <= SMARTSHEET_READ_TOOLS,
        "the duplicate Smartsheet app is disabled": duplicate.get("enabled") is False,
        "project config contains policy only, without credentials or helpers": (
            set(config) == {"mcp_servers", "apps"}
            and set(servers) == {"smartsheet"} and set(native) == {"url", "enabled_tools"}
            and set(apps) == {SMARTSHEET_APP} and set(duplicate) == {"enabled"}
        ),
    }


path = ROOT / ".codex/config.toml"
check("Codex project policy is present", path.is_file())
config = tomllib.loads(path.read_text()) if path.is_file() else {}
for name, valid in policy_checks(config).items():
    check(name, valid)

candidate = deepcopy(config)
candidate.get("mcp_servers", {}).get("smartsheet", {}).pop("enabled_tools", None)
check("the oracle rejects a missing native allowlist", not all(policy_checks(candidate).values()))
for name, server_changes, app_changes in (
    ("replacement endpoint", {"url": "https://example.invalid"}, {}),
    ("row creation", {"enabled_tools": [*SMARTSHEET_READ_TOOLS, "add_rows"]}, {}),
    ("unknown future tool", {"enabled_tools": [*SMARTSHEET_READ_TOOLS, "future_tool"]}, {}),
    ("duplicate app enabled", {}, {"enabled": True}),
    ("credential field", {"http_headers": {"Authorization": "Bearer test-only"}}, {}),
    ("credential helper", {"http_headers_helper": "test-only"}, {}),
):
    candidate = deepcopy(config)
    candidate.setdefault("mcp_servers", {}).setdefault("smartsheet", {}).update(server_changes)
    candidate.setdefault("apps", {}).setdefault(SMARTSHEET_APP, {}).update(app_changes)
    check(f"the oracle rejects {name}", not all(policy_checks(candidate).values()))

env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
for name, ignored in (
    (".codex/config.toml", False),
    (".codex/auth.json", True),
    (".codex/config.local.toml", True),
    (".codex/nested/auth.json", True),
):
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", name],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    check(f"Git {'ignores' if ignored else 'can track'} {name}",
          result.returncode == (0 if ignored else 1), result.stderr)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
