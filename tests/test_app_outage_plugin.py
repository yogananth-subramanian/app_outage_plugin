#!/usr/bin/env python
import unittest
import yaml
from arcaflow_plugin_sdk import plugin
from app_outage_plugin import app_outage_plugin
#import kubernetes_functions 


class AppOutageTest(unittest.TestCase):

    def get_kubeconfig_test_value(self, filename):
        with open(filename, "r") as f:
            test_input = f.read()
        parsed_input = yaml.safe_load(test_input)
        return parsed_input["kubeconfig"]

    def test_serialization(self):
        kubeconfig = self.get_kubeconfig_test_value("tests/test_kubeconfig.yaml")
        plugin.test_object_serialization(
            app_outage_plugin.InputParams(
                kubeconfig=kubeconfig,
                namespace='default',
                pod_name='apiserver',
                test_duration=30,
                wait_duration=30
            ),
            self.fail,
        )
        plugin.test_object_serialization(
            app_outage_plugin.AppOutageSuccessOutput(
                test_pods=['apiserver'],
                direction=['ingress'],
                ingress_ports=[80],
                egress_ports=[]),
            self.fail,
        )
        plugin.test_object_serialization(
            app_outage_plugin.AppOutageErrorOutput(
                error="Hello World",
            ),
            self.fail,
        )

    def test_app_outage(self):
        kubeconfig = self.get_kubeconfig_test_value("tests/test_kubeconfig.yaml")
        output_id, output_data = app_outage_plugin.app_outage(
            app_outage_plugin.InputParams(
                kubeconfig=kubeconfig,
                namespace='default',
                pod_name='apiserver',
                test_duration=30,
                wait_duration=30
            )
        )
        self.assertEqual("error", output_id)


if __name__ == "__main__":
    unittest.main()
