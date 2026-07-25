import unittest

import service_lines


class ServiceLineFromProjectTests(unittest.TestCase):
    def test_plain_prefixes_map_to_themselves(self):
        for prefix in ("BK", "SA", "BD"):
            self.assertEqual(
                service_lines.service_line_from_project(
                    f"{prefix} | Test client | Owner"
                ),
                prefix,
            )

    def test_ea_variants_roll_up_to_display_lines(self):
        for prefix in ("EA North", "EA UK", "EA NA"):
            self.assertEqual(
                service_lines.service_line_from_project(
                    f"{prefix} | Test client"
                ),
                "EA North",
            )
        for prefix in ("EA South", "EA S"):
            self.assertEqual(
                service_lines.service_line_from_project(
                    f"{prefix} | Test client"
                ),
                "EA South",
            )

    def test_prefix_matching_is_case_insensitive(self):
        self.assertEqual(
            service_lines.service_line_from_project("bk | Test client"), "BK"
        )
        self.assertEqual(
            service_lines.service_line_from_project("ea north | Test client"),
            "EA North",
        )
        self.assertEqual(
            service_lines.service_line_from_project("ea s | Test client"),
            "EA South",
        )

    def test_whitespace_around_the_pipe_is_ignored(self):
        self.assertEqual(
            service_lines.service_line_from_project("BK|Test client"), "BK"
        )
        self.assertEqual(
            service_lines.service_line_from_project("  BK   |   Test client"),
            "BK",
        )
        self.assertEqual(
            service_lines.service_line_from_project("EA South |Test client"),
            "EA South",
        )

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(
            service_lines.service_line_from_project("Z | Internal")
        )


class OverchargeRateLineTests(unittest.TestCase):
    def test_ea_display_lines_share_the_ea_rate(self):
        self.assertEqual(
            service_lines.overcharge_rate_line("EA North"), "EA"
        )
        self.assertEqual(
            service_lines.overcharge_rate_line("EA South"), "EA"
        )

    def test_other_lines_are_their_own_rate_key(self):
        for line in ("BK", "SA", "BD"):
            self.assertEqual(service_lines.overcharge_rate_line(line), line)


class KnownPrefixesTextTests(unittest.TestCase):
    def test_every_valid_prefix_is_listed(self):
        text = service_lines.known_prefixes_text()
        for prefix in service_lines.VALID_PREFIXES:
            self.assertIn(prefix, text)

    def test_ea_uk_and_ea_s_are_no_longer_dropped(self):
        text = service_lines.known_prefixes_text()
        self.assertIn("EA UK", text)
        self.assertIn("EA S", text)


if __name__ == "__main__":
    unittest.main()
