from network import Network
from client import AtomixClient
from logger import Logger
from errors import UnknownClusterError, UnknownNetworkError, UnknownNodeError
from six.moves import shlex_quote
import shutil
import os
import docker
import socket
from docker.api.client import APIClient
from docker.utils import kwargs_from_env

class Cluster(object):
    """Atomix test cluster."""
    def __init__(self, name):
        self.log = Logger(name, Logger.Type.FRAMEWORK)
        self.name = name
        self.network = Network(name)
        self._docker_client = docker.from_env()
        self._docker_api_client = APIClient(kwargs_from_env())

    @property
    def path(self):
        """Returns the cluster data path."""
        return os.path.join(os.getcwd(), self.name)

    def node(self, id):
        """Returns the node with the given ID."""
        if isinstance(id, int):
            return self.nodes[id-1]
        else:
            return [node for node in self.nodes if node.name == id].pop()

    @property
    def nodes(self, type=None):
        """Returns a list of nodes in the cluster."""
        # Sort the containers by name and then extract the IP address from the container info.
        if type is not None:
            labels = ['atomix-test=true', 'atomix-cluster={}'.format(self.name), 'atomix-type={}'.format(type)]
        else:
            labels = ['atomix-test=true', 'atomix-cluster={}'.format(self.name),]
        containers = sorted(self._docker_client.containers.list(all=True, filters={'label': labels}), key=lambda c: c.name)
        nodes = []
        for container in containers:
            container_info = self._docker_api_client.inspect_container(container.name)
            nodes.append(Node(container.name, container_info['NetworkSettings']['Networks'][self.network.name]['IPAddress'], container_info['Config']['Labels']['atomix-type'], self))
        return nodes

    @property
    def servers(self):
        return self.nodes(Node.Type.SERVER)

    @property
    def clients(self):
        return self.nodes(Node.Type.CLIENT)

    def _node_name(self, id):
        return '{}-{}'.format(self.name, id)

    def setup(self, nodes=3, subnet='172.18.0.0/16', gateway=None):
        """Sets up the cluster."""
        self.log.message("Setting up cluster")

        # Set up the test network.
        self.network.setup(subnet, gateway)

        # Iterate through nodes and setup containers.
        for n in range(1, nodes + 1):
            Node(self._node_name(n), next(self.network.hosts), Node.Type.SERVER, self).setup()

    def add_node(self, type='server'):
        """Adds a new node to the cluster."""
        self.log.message("Adding a node to the cluster")
        Node(self._node_name(len(self.nodes)+1), next(self.network.hosts), type, self).setup()

    def remove_node(self, id):
        """Removes a node from the cluster."""
        self.log.message("Removing a node from the cluster")
        self.node(id).teardown()

    def teardown(self):
        """Tears down the cluster."""
        self.log.message("Tearing down cluster")
        for node in self.nodes:
            try:
                node.teardown()
            except UnknownNodeError, e:
                self.log.error(str(e))
        try:
            self.network.teardown()
        except UnknownNetworkError, e:
            self.log.error(str(e))

    def cleanup(self):
        """Cleans up the cluster data."""
        self.log.message("Cleaning up cluster state")
        if os.path.exists(self.path):
            shutil.rmtree(self.path)

    def __str__(self):
        lines = []
        lines.append('name: {}'.format(self.name))
        lines.append('network:')
        lines.append('  name: {}'.format(self.network.name))
        lines.append('  subnet: {}'.format(self.network.subnet))
        lines.append('  gateway: {}'.format(self.network.gateway))
        lines.append('nodes:')
        for node in self.nodes:
            lines.append('  {}:'.format(node.name))
            lines.append('    state: {}'.format(node.docker_container.status))
            lines.append('    type: {}'.format(node.type))
            lines.append('    ip: {}'.format(node.ip))
            lines.append('    host port: {}'.format(node.local_port))
        return '\n'.join(lines)


class Node(object):
    """Atomix test node."""
    class Type(object):
        SERVER = 'server'
        CLIENT = 'client'

    def __init__(self, name, ip, type, cluster):
        self.log = Logger(cluster.name, Logger.Type.FRAMEWORK)
        self.name = name
        self.ip = ip
        self.type = type
        self.http_port = 5678
        self.tcp_port = 5679
        self.cluster = cluster
        self.path = os.path.join(self.cluster.path, self.name)
        self.client = AtomixClient(self)
        self._docker_client = docker.from_env()
        self._docker_api_client = APIClient(kwargs_from_env())

    def _find_open_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
        s.close()
        return port

    @property
    def local_port(self):
        port_bindings = self._docker_api_client.inspect_container(self.docker_container.name)['HostConfig']['PortBindings']
        return port_bindings['{}/tcp'.format(self.http_port)][0]['HostPort']

    @property
    def docker_container(self):
        try:
            return self._docker_client.containers.get(self.name)
        except docker.errors.NotFound:
            raise UnknownNodeError(self.name)

    def setup(self):
        """Sets up the node."""
        args = []
        args.append('%s:%s:%d' % (self.name, self.ip, self.tcp_port))
        args.append('--bootstrap')
        for node in self.cluster.nodes:
            args.append('%s:%s:%d' % (node.name, node.ip, node.tcp_port))

        self.log.message("Running container {}", self.name)
        self._docker_client.containers.run(
            'atomix',
            ' '.join(args),
            name=self.name,
            labels={'atomix-test': 'true', 'atomix-cluster': self.cluster.name, 'atomix-type': self.type},
            network=self.cluster.network.name,
            ports={self.http_port: self._find_open_port()},
            detach=True,
            volumes={self.path: {'bind': '/data', 'mode': 'rw'}})

    def run(self, *command):
        """Runs the given command in the container."""
        command = ' '.join([shlex_quote(arg) for arg in command])
        self.log.message("Executing command '{}' on {}", command, self.name)
        return self.docker_container.exec_run(command)

    def stop(self):
        """Stops the node."""
        self.log.message("Stopping node {}", self.name)
        self.docker_container.stop()

    def start(self):
        """Starts the node."""
        self.log.message("Starting node {}", self.name)
        self.docker_container.start()

    def kill(self):
        """Kills the node."""
        self.log.message("Killing node {}", self.name)
        self.docker_container.kill()

    def recover(self):
        """Recovers a killed node."""
        self.log.message("Recovering node {}", self.name)
        self.docker_container.start()

    def restart(self):
        """Restarts the node."""
        self.log.message("Restarting node {}", self.name)
        self.docker_container.restart()

    def partition(self, node):
        """Partitions this node from the given node."""
        self.cluster.network.partition(self, node)

    def heal(self, node):
        """Heals a partition between this node and the given node."""
        self.cluster.network.heal(self, node)

    def isolate(self):
        """Isolates this node from all other nodes in the cluster."""
        self.cluster.network.isolate(self)

    def unisolate(self):
        """Unisolates this node from all other nodes in the cluster."""
        self.cluster.network.unisolate(self)

    def delay(self, latency=50, jitter=10, correlation=.75, distribution='normal'):
        """Delays packets to this node."""
        self.cluster.network.delay(self, latency, jitter, correlation, distribution)

    def drop(self, probability=.02, correlation=.25):
        """Drops packets to this node."""
        self.cluster.network.drop(self, probability, correlation)

    def reorder(self, probability=.02, correlation=.5):
        """Reorders packets to this node."""
        self.cluster.network.reorder(self, probability, correlation)

    def duplicate(self, probability=.005, correlation=.05):
        """Duplicates packets to this node."""
        self.cluster.network.duplicate(self, probability, correlation)

    def corrupt(self, probability=.02):
        """Duplicates packets to this node."""
        self.cluster.network.corrupt(self, probability)

    def restore(self):
        """Restores packets to this node to normal order."""
        self.cluster.network.restore(self)

    def teardown(self):
        """Tears down the node."""
        container = self.docker_container
        self.log.message("Stopping container {}", self.name)
        container.stop()
        self.log.message("Removing container {}", self.name)
        container.remove()

    def wait(self):
        """Waits for the node to exit."""
        self.docker_container.wait()

cluster = None

def set_cluster(name):
    global cluster
    cluster = get_cluster(name)

def _find_cluster():
    docker_client = docker.from_env()
    docker_api_client = APIClient(kwargs_from_env())
    containers = docker_client.containers.list(filters={'label': 'atomix-test=true'})
    if len(containers) > 0:
        container = containers[0]
        cluster_name = docker_api_client.inspect_container(container.name)['Config']['Labels']['atomix-cluster']
        return Cluster(cluster_name)
    raise UnknownClusterError

def get_cluster(name=None):
    if name is not None:
        return Cluster(name)
    elif cluster is not None:
        return cluster
    else:
        return _find_cluster()

def node(id):
    return cluster.node(id)

def nodes():
    return cluster.nodes