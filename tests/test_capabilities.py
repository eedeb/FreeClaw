"""What the agent knows it can do (src/agent.py capability_block), and the
proactive rule that comes with it.

    python -m unittest tests.test_capabilities
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import agent  # noqa: E402


def tool(name):
    return {"type": "function", "function": {"name": name}}


BROWSER = {"name": "browser", "can": "use a real web browser: open sites, read them, fill forms",
           "description": "A web browser the agent drives by screenshot. Watch it live."}
WEATHER = {"name": "weather", "description": "Forecasts for any city. Also alerts."}
HIDDEN = {"name": "router", "can": "", "description": "Internal."}
CATALOGUE = [tool(n) for n in ("web_search", "add_ping", "create_file", "read_file", "mcp_browser_navigate",
                                "mcp_weather_forecast", "mcp_router_x")]
REGISTRY = {"mcp_browser_navigate": {"server": BROWSER}, "mcp_browser_click": {"server": BROWSER},
            "mcp_weather_forecast": {"server": WEATHER}, "mcp_router_x": {"server": HIDDEN}}


class TestCapabilities(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(agent, "tools_for", lambda user: CATALOGUE),
                   mock.patch.object(agent, "registry_for", lambda user: REGISTRY),
                   mock.patch.object(agent, "CAPABILITY_HOOKS", [])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_built_in_tools_it_has_in_plain_words(self):
        block = agent.capability_block("acme__Talon")
        for phrase in ("search the web", "schedule tasks", "make files"):
            self.assertIn(phrase, block)
        self.assertNotIn("make images", block)            # not in its catalogue
        self.assertNotIn("sub-agents", block)

    def test_servers(self):
        block = agent.capability_block("acme__Talon")
        self.assertEqual(block.count("browser: use a real web browser"), 1)
        self.assertIn("weather: Forecasts for any city", block)     # first sentence
        self.assertNotIn("Also alerts", block)
        self.assertNotIn("router", block)                           # can: "" says nothing

    def test_host_hooks(self):
        agent.CAPABILITY_HOOKS.append(lambda user: ["your connected apps: Gmail, Google Calendar"])
        agent.CAPABILITY_HOOKS.append(lambda user: 1 / 0)
        block = agent.capability_block("acme__Talon")
        self.assertIn("- your connected apps: Gmail, Google Calendar", block)
        self.assertIn("search the web", block)                      # a failing hook takes nothing down

    def test_stable_for_the_prompt_cache(self):
        self.assertEqual(agent.capability_block("a"), agent.capability_block("a"))

    def test_proactive_rule(self):
        block = agent.capability_block("acme__Talon")
        main = agent._instructions(capabilities=block)
        sub = agent._instructions(subagent=True, capabilities=block)
        self.assertIn(agent.PROACTIVE_INSTRUCTION, main)
        self.assertIn(block, main)
        self.assertIn(block, sub)                                   # knows what it can do…
        self.assertNotIn(agent.PROACTIVE_INSTRUCTION, sub)          # …but reports to its main agent
        self.assertNotIn(agent.PROACTIVE_INSTRUCTION, agent._instructions())
        self.assertLess((len(main) - len(agent._instructions())) / 4, 250)   # tokens, roughly

    def test_unreadable_catalogue(self):
        with mock.patch.object(agent, "tools_for", side_effect=RuntimeError("no catalogue")):
            self.assertEqual(agent.capability_block("x"), "")


if __name__ == "__main__":
    unittest.main()
