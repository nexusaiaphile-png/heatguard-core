import unittest

from edge_agent import parse_thermal_payload


class ThermalPayloadTests(unittest.TestCase):
    def test_nested_json(self):
        parsed = parse_thermal_payload(
            '{"Thermal":{"maxTemperature":"82.5","averageTemperature":"74.5","minTemperature":"60.0"}}'
        )
        self.assertEqual(parsed["max_temp"], 82.5)
        self.assertEqual(parsed["avg_temp"], 74.5)
        self.assertEqual(parsed["min_temp"], 60.0)

    def test_namespaced_xml(self):
        parsed = parse_thermal_payload(
            '<Thermal xmlns="urn:test"><maxTemperature>82</maxTemperature>'
            '<averageTemperature>74.5</averageTemperature><minTemperature>61</minTemperature>'
            '<channelName>Freezer</channelName></Thermal>'
        )
        self.assertEqual(parsed["avg_temp"], 74.5)
        self.assertEqual(parsed["location"], "Freezer")

    def test_missing_temperature_rejected(self):
        with self.assertRaises(ValueError):
            parse_thermal_payload('{"averageTemperature": 74.5}')

    def test_non_numeric_temperature_rejected(self):
        with self.assertRaises(ValueError):
            parse_thermal_payload(
                '{"maxTemperature":"hot","averageTemperature":"warm","minTemperature":"cold"}'
            )

    def test_empty_payload_rejected(self):
        with self.assertRaises(ValueError):
            parse_thermal_payload("  ")


if __name__ == "__main__":
    unittest.main()
