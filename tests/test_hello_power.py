"""hello_with_power: the saved hello follows the radio's start;/stop; messages."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tci_link import TciLink, hello_with_power  # noqa: E402


class HelloPowerTests(unittest.TestCase):
    def test_a_stale_start_becomes_stop_and_back(self):
        hello = ["protocol:ExpertSDR3,2.1;", "start;", "trx_count:2;", "ready;"]
        stopped = hello_with_power(hello, False)
        self.assertEqual(stopped, ["protocol:ExpertSDR3,2.1;", "stop;", "trx_count:2;", "ready;"])
        self.assertEqual(hello_with_power(stopped, True), hello)
        self.assertEqual(hello[1], "start;")                       # the input list is not changed

    def test_a_power_message_inside_a_longer_message_is_replaced(self):
        self.assertEqual(hello_with_power(["a:1;start;b:2;", "ready;"], False), ["a:1;stop;b:2;", "ready;"])

    def test_commands_that_only_contain_start_are_left_alone(self):
        hello = ["iq_start:0;", "audio_start:1;", "start;", "ready;"]
        self.assertEqual(hello_with_power(hello, False), ["iq_start:0;", "audio_start:1;", "stop;", "ready;"])

    def test_a_missing_power_message_is_inserted_before_ready(self):
        self.assertEqual(hello_with_power(["a:1;", "ready;"], True), ["a:1;", "start;", "ready;"])
        self.assertEqual(hello_with_power(["a:1;"], False), ["a:1;", "stop;"])

    def test_the_link_updates_its_hello_on_a_live_stop_and_start(self):
        link = TciLink("r", "127.0.0.1", 1, events=None)
        link.hello = ["x:1;", "start;", "ready;"]
        link.hello = hello_with_power(link.hello, False)
        self.assertIn("stop;", link.hello)
        link.hello = hello_with_power(link.hello, True)
        self.assertEqual(link.hello, ["x:1;", "start;", "ready;"])


if __name__ == "__main__":
    unittest.main()
