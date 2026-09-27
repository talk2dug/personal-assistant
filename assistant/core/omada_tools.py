"""Chat-facing tool schemas for the Omada network integration -- same split as
git_tools.py: the schema list and system note live here, the actual client lives in
omada_client.py.

Read tools are free. `reboot_omada_device` is listed in OMADA_SENSITIVE_TOOLS and MUST be
wired through engine.py's existing sensitive-tools confirmation gate (the same one guarding
git_merge_pr, Home Assistant's lock/cover/alarm domains, Kroger checkout, and CCXT trades)
-- never called directly without that confirmation round-trip. See omada_client.py's
module docstring for why this is deliberately a narrower control surface than "block a
client", and docs/omada-network-design.md for the fuller design.
"""

OMADA_TOOLS = [
    {"type": "function", "function": {
        "name": "list_omada_devices",
        "description": (
            "List every network device (access point, switch, gateway) the Omada "
            "controller manages -- name, model, IP, online/offline status. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_omada_clients",
        "description": (
            "List every client currently on the network (wired and wireless) -- name, "
            "IP, MAC, which AP/SSID a wireless client is on. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_omada_network_health",
        "description": (
            "How many devices/clients are online right now, and which devices (if any) "
            "are currently offline -- the same live picture host_health.py gives for "
            "SSH-managed hosts, at the network layer instead."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "reboot_omada_device",
        "description": (
            "Reboot one network device (AP, switch, or gateway) by MAC address. This "
            "actually interrupts network service for whatever's connected through that "
            "device -- it requires the owner's explicit confirmation before it runs, the "
            "same as a GitHub PR merge or a real-money trade. Never claim this has "
            "happened unless it was actually confirmed and executed."
        ),
        "parameters": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "The device's MAC address, from list_omada_devices."},
        }, "required": ["mac"]},
    }},
]

OMADA_TOOL_NAMES = {t["function"]["name"] for t in OMADA_TOOLS}

# Only the one control action -- see omada_client.py's module docstring for why the
# surface starts this narrow.
OMADA_SENSITIVE_TOOLS = {"reboot_omada_device"}

OMADA_SYSTEM_NOTE = (
    " You also have visibility into the owner's Omada-managed network: list_omada_devices "
    "and list_omada_clients show what's actually there right now, and "
    "get_omada_network_health gives the same up/down picture for network devices that "
    "host-health monitoring gives for SSH hosts. reboot_omada_device is the one control "
    "action available -- it interrupts real network service, so it requires the owner's "
    "explicit confirmation before it executes, same as merging a PR or placing a real "
    "trade. Don't propose it lightly, and never claim a device was rebooted unless the "
    "confirmation actually happened and the call actually ran."
)
