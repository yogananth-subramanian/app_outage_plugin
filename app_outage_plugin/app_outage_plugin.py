#!/usr/bin/env python3
import sys
import os
import typing
import yaml
import logging
import time
import random
from requests.packages import urllib3
from dataclasses import dataclass, field
from traceback import format_exc
from jinja2 import Environment, FileSystemLoader
from arcaflow_plugin_sdk import plugin, validation
from kubernetes import client
from kubernetes.client.api.core_v1_api import CoreV1Api
from kubernetes.client.api.batch_v1_api import BatchV1Api
from kubernetes.client.api.apiextensions_v1_api import ApiextensionsV1Api
from app_outage_plugin import kubernetes_functions as kube_helper 

def get_test_pods(
    pod_name: str,
    pod_label: str,
    namespace: str,
    cli: CoreV1Api
) -> typing.List[str]:
    """
    Function that returns a list of pods to apply network policy

    Args:
        pod_name (string)
            - pod on which network policy need to be applied

        pod_label (string)
            - pods matching the label on which network policy
              need to be applied

        namepsace (string)
            - namespace in which the pod is present

        cli (CoreV1Api)
            - Object to interact with Kubernetes Python client's CoreV1 API

    Returns:
        pod names (string) in the namespace
    """
    pods_list = []
    pods_list = kube_helper.list_pods(
        cli,
        label_selector=pod_label,
        namespace=namespace
    )
    if pod_name and pod_name not in pods_list:
        raise Exception(
            "pod name not found in namespace "
        )
    elif pod_name and pod_name in pods_list:
        pods_list.clear()
        pods_list.append(pod_name)
        return pods_list
    else:
        return pods_list


def get_job_pods(cli: CoreV1Api, api_response):
    """
    Function that gets the pod corresponding to the job

    Args:
        cli (CoreV1Api)
            - Object to interact with Kubernetes Python client's CoreV1 API

        api_response
            - The API response for the job status

    Returns
        Pod corresponding to the job
    """

    controllerUid = api_response.metadata.labels["controller-uid"]
    pod_label_selector = "controller-uid=" + controllerUid
    pods_list = kube_helper.list_pods(
        cli,
        label_selector=pod_label_selector,
        namespace="default"
    )

    return pods_list[0]


def delete_jobs(
    cli: CoreV1Api,
    batch_cli: BatchV1Api,
    job_list: typing.List[str]
):
    """
    Function that deletes jobs

    Args:
        cli (CoreV1Api)
            - Object to interact with Kubernetes Python client's CoreV1 API

        batch_cli (BatchV1Api)
            - Object to interact with Kubernetes Python client's BatchV1 API

        job_list (List of strings)
            - The list of jobs to delete
    """

    for job_name in job_list:
        try:
            api_response = kube_helper.get_job_status(
                batch_cli,
                job_name,
                namespace="default"
            )
            if api_response.status.failed is not None:
                pod_name = get_job_pods(cli, api_response)
                pod_stat = kube_helper.read_pod(
                    cli,
                    name=pod_name,
                    namespace="default"
                )
                logging.error(pod_stat.status.container_statuses)
                pod_log_response = kube_helper.get_pod_log(
                    cli,
                    name=pod_name,
                    namespace="default"
                )
                pod_log = pod_log_response.data.decode("utf-8")
                logging.error(pod_log)
        except Exception as e:
            logging.warn("Exception in getting job status: %s" % str(e))
        api_response = kube_helper.delete_job(
            batch_cli,
            name=job_name,
            namespace="default"
        )


def wait_for_job(
    batch_cli: BatchV1Api,
    job_list: typing.List[str],
    timeout: int = 300
) -> None:
    """
    Function that waits for a list of jobs to finish within a time period

    Args:
        batch_cli (BatchV1Api)
            - Object to interact with Kubernetes Python client's BatchV1 API

        job_list (List of strings)
            - The list of jobs to check for completion

        timeout (int)
            - Max duration to wait for checking whether the jobs are completed
    """

    wait_time = time.time() + timeout
    count = 0
    job_len = len(job_list)
    while count != job_len:
        for job_name in job_list:
            try:
                api_response = kube_helper.get_job_status(
                    batch_cli,
                    job_name,
                    namespace="default"
                )
                if (
                    api_response.status.succeeded is not None or
                    api_response.status.failed is not None
                ):
                    count += 1
                    job_list.remove(job_name)
            except Exception:
                logging.warn("Exception in getting job status")
            if time.time() > wait_time:
                raise Exception(
                    "Jobs did not complete within "
                    "the {0}s timeout period".format(timeout)
                )
            time.sleep(5)


def get_bridge_name(
    cli: ApiextensionsV1Api
) -> str:
    current_crds = [x['metadata']['name'].lower()
                    for x in cli.list_custom_resource_definition().to_dict()['items']]
    if 'networks.config.openshift.io' not in current_crds:
        raise Exception(
            "OpenShiftSDN or OVNKubernetes not found in cluster "
        )
    else:
        api = client.CustomObjectsApi()
        resource = api.get_cluster_custom_object(
            group="config.openshift.io", version="v1", name="cluster", plural="networks")
        network_type = resource["spec"]["networkType"]
        if network_type == 'OpenShiftSDN':
            bridge = 'br0'
        elif network_type == 'OVNKubernetes':
            bridge = 'br-int'
        else:
            raise Exception(
                f'OpenShiftSDN or OVNKubernetes not found in cluster {network_type}'
            )
    return bridge


def get_ports(
    test_namespace: str,
    label_set: set,
    cli: CoreV1Api
) -> str:
    service_lists = kube_helper.list_service(cli, test_namespace)
    for service in service_lists:
        temp_set = set()
        service_stat = kube_helper.read_service(cli, service, test_namespace)
        if not service_stat.spec.selector:
            continue
        for key, value in service_stat.spec.selector.items():
            temp_set.add("%s=%s" % (key, value))
        if label_set.intersection(temp_set):
            test_label = label_set.intersection(temp_set)
            test_service = service_stat.metadata.name
    port_lst = [port.target_port for port in kube_helper.read_service(
        cli, test_service, test_namespace).spec.ports]
    return port_lst


def apply_net_policy(
    node_dict: typing.Dict[str, str],
    ports: typing.List[str],
    job_template,
    pod_template,
    direction: str,
    duration: str,
    bridge_name: str,
    cli: CoreV1Api,
    batch_cli: BatchV1Api
) -> typing.List[str]:
    job_list = []
    cookie = random.randint(100, 10000)
    net_direction = {'egress': 'nw_src', 'ingress': 'nw_dst'}
    br = 'br0'
    table = 0
    if bridge_name == 'br-int':
        br = 'br-int'
        table = 8
    for node, ips in node_dict.items():
        while len(check_cookie(node, pod_template, br, cookie, cli)) > 2:
            cookie = random.randint(100, 10000)
        exec_cmd = ''
        for ip in ips:
            for port in ports:
                target_port = port
                exec_cmd = f'{exec_cmd}ovs-ofctl -O  OpenFlow13 add-flow {br} cookie={cookie},table={table},priority=65535,tcp,{net_direction[direction]}={ip},tp_dst={target_port},actions=drop;'
                exec_cmd = f'{exec_cmd}ovs-ofctl -O  OpenFlow13 add-flow {br} cookie={cookie},table={table},priority=65535,udp,{net_direction[direction]}={ip},tp_dst={target_port},actions=drop;'
            if not ports:
                exec_cmd = f'{exec_cmd}ovs-ofctl -O  OpenFlow13 add-flow {br} cookie={cookie},table={table},priority=65535,ip,{net_direction[direction]}={ip},actions=drop;'
        exec_cmd = f'{exec_cmd}sleep {duration};ovs-ofctl -O  OpenFlow13  del-flows {br} cookie={cookie}/-1'
        job_body = yaml.safe_load(
            job_template.render(
                jobname=str(hash(node))[:5],
                nodename=node,
                cmd=exec_cmd
            )
        )
        api_response = kube_helper.create_job(batch_cli, job_body)
        if api_response is None:
            raise Exception("Error creating job")

        job_list.append(job_body["metadata"]["name"])
    return job_list


def get_default_interface(
    node: str,
    pod_template,
    cli: CoreV1Api
) -> typing.List[str]:
    """
    Function that returns a random interface from a node

    Args:
        node (string)
            - Node from which the interface is to be returned

        pod_template (jinja2.environment.Template)
            - The YAML template used to instantiate a pod to query
              the node's interface

        cli (CoreV1Api)
            - Object to interact with Kubernetes Python client's CoreV1 API

    Returns:
        Default interface (string) belonging to the node
    """

    pod_body = yaml.safe_load(pod_template.render(nodename=node))
    logging.info("Creating pod to query interface on node %s" % node)
    kube_helper.create_pod(cli, pod_body, "default", 300)

    try:
        cmd = ["chroot", "/host", "ovs-vsctl", "list-br"]
        output = kube_helper.exec_cmd_in_pod(cli, cmd, "modtools", "default")

        if not output:
            logging.error("Exception occurred while executing command in pod")
            sys.exit(1)

        bridges = output.split('\n')

    finally:
        logging.info("Deleting pod to query interface on node")
        kube_helper.delete_pod(cli, "modtools", "default")

    return bridges


def check_cookie(
    node: str,
    pod_template,
    br_name,
    cookie,
    cli: CoreV1Api
) -> str:
    pod_body = yaml.safe_load(pod_template.render(nodename=node))
    logging.info("Creating pod to query interface on node %s" % node)
    kube_helper.create_pod(cli, pod_body, "default", 300)

    try:
        cmd = ["chroot", "/host", "ovs-ofctl", "-O", "OpenFlow13",
               "dump-flows", br_name, f'cookie={cookie}/-1']
        output = kube_helper.exec_cmd_in_pod(cli, cmd, "modtools", "default")

        if not output:
            logging.error("Exception occurred while executing command in pod")
            sys.exit(1)

        flow_list = output.split('\n')

    finally:
        logging.info("Deleting pod to query interface on node")
        kube_helper.delete_pod(cli, "modtools", "default")

    return flow_list


def check_bridge_interface(
    node_name: str,
    label_selector: str,
    instance_count: int,
    pod_template,
    bridge_name,
    cli: CoreV1Api
) -> bool:
    """
    Function that is used to process the input dictionary with the nodes and
    its test interfaces.

    If the dictionary is empty, the label selector is used to select the nodes,
    and then a random interface on each node is chosen as a test interface.

    If the dictionary is not empty, it is filtered to include the nodes which
    are active and then their interfaces are verified to be present

    Args:
        node_interface_dict (Dictionary with keys as node name and value as
        a list of interface names)
            - Nodes and their interfaces for the scenario

        label_selector (string):
            - Label selector to get nodes if node_interface_dict is empty

        instance_count (int):
            - Number of nodes to fetch in case node_interface_dict is empty

        pod_template (jinja2.environment.Template)
            - The YAML template used to instantiate a pod to query
              the node's interfaces

        cli (CoreV1Api)
            - Object to interact with Kubernetes Python client's CoreV1 API

    Returns:
        Filtered dictionary containing the test nodes and their test interfaces
    """
    nodes = kube_helper.get_node(node_name, None, instance_count, cli)
    node_bridge = []
    for node in nodes:
        node_bridge = get_default_interface(
            node,
            pod_template,
            cli
        )
    if bridge_name not in node_bridge:
        raise Exception(
            f'OVS bridge {bridge_name} not found on the node '
        )

    return True


@dataclass
class InputParams:
    """
    This is the data structure for the input parameters of the step defined below.
    """
    namespace: typing.Annotated[str, validation.min(1)] = field(
        metadata={
            "name": "Label selector",
            "description":
                "Kubernetes label selector for the target nodes. "
                "Required if node_interface_name is not set.\n"
                "See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/ "  # noqa
                "for details.",
        }
    )

    direction: typing.List[str] = field(
        default_factory=lambda: ['ingress', 'egress'],
        metadata={
            "name": "Node Interface Name",
            "description":
                "Dictionary with node names as key and values as a list of "
                "their test interfaces. "
                "Required if label_selector is not set.",
        }
    )

    ingress_ports: typing.List[int] = field(
        default_factory=list,
        metadata={
            "name": "Traffic direction selector",
            "description":
                "Kubernetes label selector for the target nodes. "
                "Required if node_interface_name is not set.\n"
                "See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/ "  # noqa
                "for details.",
        }
    )

    egress_ports: typing.List[int] = field(
        default_factory=list,
        metadata={
            "name": "Traffic direction selector",
            "description":
                "Kubernetes label selector for the target nodes. "
                "Required if node_interface_name is not set.\n"
                "See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/ "  # noqa
                "for details.",
        }
    )
    kubeconfig: typing.Optional[str] = field(
        default=None,
        metadata={
            "name": "Kubeconfig path",
            "description": "Kubeconfig file as string\n"
            "See https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/ for "
            "details.",
        },
    )
    pod_name: typing.Annotated[
        typing.Optional[str], validation.required_if_not("label_selector"),
    ] = field(
        default=None,
        metadata={
            "name": "Node Interface Name",
            "description":
                "Dictionary with node names as key and values as a list of "
                "their test interfaces. "
                "Required if label_selector is not set.",
        }
    )

    label_selector: typing.Annotated[
        typing.Optional[str], validation.required_if_not("pod_name")
    ] = field(
        default=None,
        metadata={
            "name": "Label selector",
            "description":
                "Kubernetes label selector for the target nodes. "
                "Required if node_interface_name is not set.\n"
                "See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/ "  # noqa
                "for details.",
        }
    )

    test_duration: typing.Annotated[
        typing.Optional[int],
        validation.min(1)
    ] = field(
        default=120,
        metadata={
            "name": "Test duration",
            "description":
                "Duration for which each step of the ingress chaos testing "
                "is to be performed.",
        },
    )

    wait_duration: typing.Annotated[
        typing.Optional[int],
        validation.min(1)
    ] = field(
        default=300,
        metadata={
            "name": "Wait Duration",
            "description":
                "Wait duration for finishing a test and its cleanup."
                "Ensure that it is significantly greater than wait_duration"
        }
    )

    instance_count: typing.Annotated[
        typing.Optional[int],
        validation.min(1)
    ] = field(
        default=1,
        metadata={
            "name": "Instance Count",
            "description":
                "Number of nodes to perform action/select that match "
                "the label selector.",
        }
    )


@dataclass
class AppOutageSuccessOutput:
    """
    This is the output data structure for the success case.
    """

    test_pods: typing.List[str] = field(
        metadata={
            "name": "Filter Direction",
            "description":
                "Direction in which the traffic control filters are applied "
                "on the test interfaces"
        }
    )

    direction: typing.List[str] = field(
        metadata={
            "name": "Test Interfaces",
            "description":
                "Dictionary of nodes and their interfaces on which "
                "the chaos experiment was performed"
        }
    )

    ingress_ports: typing.List[int] = field(
        metadata={
            "name": "Network Parameters",
            "description":
                "The network filters that are applied on the interfaces"
        }
    )

    egress_ports: typing.List[int] = field(
        metadata={
            "name": "Execution Type",
            "description": "The order in which the filters are applied"
        }
    )


@dataclass
class AppOutageErrorOutput:
    error: str = field(
        metadata={
            "name": "Error",
            "description":
                "Error message when there is a run-time error during "
                "the execution of the scenario"
        }
    )

# The following is a decorator (starting with @). We add this in front of our function to define the metadata for our
# step.


@plugin.step(
    id="app_outage",
    name="App Outage",
    description="Says hello :)",
    outputs={"success": AppOutageSuccessOutput, "error": AppOutageErrorOutput},
)
def app_outage(
    params: InputParams,
) -> typing.Tuple[str, typing.Union[AppOutageSuccessOutput, AppOutageErrorOutput]]:
    """
    The function  is the implementation for the step. It needs the decorator above to make it into a  step. The type
    hints for the params are required.

    :param params:

    :return: the string identifying which output it is, as well the output structure
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    direction = ['ingress', 'egress']
    file_loader = FileSystemLoader(os.path.abspath(os.path.dirname(__file__)))
    env = Environment(loader=file_loader)
    job_template = env.get_template("job.j2")
    pod_module_template = env.get_template("pod_module.j2")
    test_namespace = params.namespace
    test_label_selector = params.label_selector
    test_pod_name = params.pod_name
    filter_dict = {}
    job_list = []
    for i in params.direction:
        filter_dict[i] = eval(f"params.{i}_ports")
    try:
        ip_set = set()
        node_dict = {}
        label_set = set()

        api = kube_helper.setup_kubernetes(params.kubeconfig)
        cli = client.CoreV1Api(api)
        batch_cli = client.BatchV1Api(api)
        api_ext = client.ApiextensionsV1Api(api)

        br_name = get_bridge_name(api_ext)
        pods_list = get_test_pods(
            test_pod_name, test_label_selector, test_namespace, cli)

        while not len(pods_list) <= params.instance_count:
            pods_list.pop(random.randint(0, len(pods_list)-1))

        for pod_name in pods_list:
            pod_stat = kube_helper.read_pod(cli, pod_name, test_namespace)
            ip_set.add(pod_stat.status.pod_ip)
            node_dict.setdefault(pod_stat.spec.node_name, [])
            node_dict[pod_stat.spec.node_name].append(pod_stat.status.pod_ip)
            for key, value in pod_stat.metadata.labels.items():
                label_set.add("%s=%s" % (key, value))

        node_interface_dict = check_bridge_interface(
            list(node_dict.keys())[0], None, 1, pod_module_template, br_name, cli)
        port_lst = get_ports(test_namespace, label_set, cli)

        for direction, ports in filter_dict.items():
            job_list = apply_net_policy(node_dict, ports, job_template, pod_module_template,
                                        direction, params.test_duration, br_name, cli, batch_cli)

        start_time = int(time.time())
        logging.info("Waiting for job to finish")
        wait_for_job(batch_cli, job_list[:], params.wait_duration+20)
        logging.info("Deleting jobs")
        delete_jobs(cli, batch_cli, job_list[:])
        logging.info(
            "Waiting for wait_duration : %ss" % params.wait_duration
        )
        time.sleep(params.wait_duration)
        end_time = int(time.time())

        return "success", AppOutageSuccessOutput(
            test_pods=pods_list,
            direction=params.direction,
            ingress_ports=params.ingress_ports,
            egress_ports=params.egress_ports
        )
    except Exception:
        logging.info("Deleting jobs(if any)")
        delete_jobs(cli, batch_cli, job_list[:])
        return "error", AppOutageErrorOutput(
            format_exc()
        )


if __name__ == "__main__":
    sys.exit(
        plugin.run(
            plugin.build_schema(
                app_outage,
            )
        )
    )
