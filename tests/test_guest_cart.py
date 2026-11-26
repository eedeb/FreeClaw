"""Accounts and checkout (src/agent.py ACCOUNT_ACTIONS_INSTRUCTION): sign in
first, unless the person said to use a guest cart or has no account there.

    python -m unittest tests.test_guest_cart
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import agent  # noqa: E402


class TestGuestCart(unittest.TestCase):
    def test_sign_in_first_is_still_the_rule(self):
        self.assertIn("call request_sign_in for that site before doing anything else",
                      agent.ACCOUNT_ACTIONS_INSTRUCTION)

    def test_but_a_guest_cart_they_asked_for_goes_ahead(self):
        rule = agent.ACCOUNT_ACTIONS_INSTRUCTION
        self.assertIn("unless they said to use a guest cart", rule)
        self.assertIn("they have no account there", rule)
        self.assertIn("the cart is only in your browser", rule)

    def test_still_never_checks_out(self):
        self.assertIn("Never check out, pay", agent.ACCOUNT_ACTIONS_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
