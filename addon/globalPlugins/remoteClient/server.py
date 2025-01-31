import logging
from .serializer import JSONSerializer
import os
import socket
import ssl
import sys
import time
from select import select
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


class LocalRelayServer:
    PING_TIME: int = 300
    _running: bool = False
    port: int
    password: str
    clients: Dict[socket.socket, 'Client']
    clientSockets: List[socket.socket]
    serverSocket: ssl.SSLSocket
    serverSocket6: ssl.SSLSocket
    lastPingTime: float

    def __init__(self, port: int, password: str, bind_host: str = '', bind_host6: str = '[::]:'):
        self.port = port
        self.password = password
        self.serializer = JSONSerializer()
        # Maps client sockets to clients
        self.clients = {}
        self.clientSockets = []
        self._running = False
        self.serverSocket = self.createServerSocket(
            socket.AF_INET, socket.SOCK_STREAM, bind_addr=(bind_host, self.port))
        self.serverSocket6 = self.createServerSocket(
            socket.AF_INET6, socket.SOCK_STREAM, bind_addr=(bind_host6, self.port))

    def createServerSocket(self, family: int, type: int, bind_addr: Tuple[str, int]) -> ssl.SSLSocket:
        serverSocket = socket.socket(family, type)
        certfile = os.path.join(os.path.abspath(
            os.path.dirname(__file__)), 'server.pem')
        serverSocket = ssl.wrap_socket(serverSocket, certfile=certfile)
        serverSocket.bind(bind_addr)
        serverSocket.listen(5)
        return serverSocket

    def run(self) -> None:
        self._running = True
        self.lastPingTime = time.time()
        while self._running:
            r, w, e = select(
                self.clientSockets+[self.serverSocket, self.serverSocket6], [], self.clientSockets, 60)
            if not self._running:
                break
            for sock in r:
                if sock is self.serverSocket or sock is self.serverSocket6:
                    self.acceptNewConnection(sock)
                    continue
                self.clients[sock].handleData()
            if time.time() - self.lastPingTime >= self.PING_TIME:
                for client in self.clients.values():
                    if client.authenticated:
                        client.send(type='ping')
                self.lastPingTime = time.time()

    def acceptNewConnection(self, sock: ssl.SSLSocket) -> None:
        try:
            clientSock, addr = sock.accept()
        except (ssl.SSLError, socket.error, OSError):
            logger.exception('Error accepting connection')
            return
        clientSock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = Client(server=self, socket=clientSock)
        self.addClient(client)

    def addClient(self, client: 'Client') -> None:
        self.clients[client.socket] = client
        self.clientSockets.append(client.socket)

    def removeClient(self, client: 'Client') -> None:
        del self.clients[client.socket]
        self.clientSockets.remove(client.socket)

    def clientDisconnected(self, client: 'Client') -> None:
        self.removeClient(client)
        if client.authenticated:
            client.send_to_others(type='client_left',
                                  user_id=client.id, client=client.asDict())

    def close(self) -> None:
        self._running = False
        self.serverSocket.close()
        self.serverSocket6.close()


class Client:
    id: int = 0
    server: LocalRelayServer
    socket: ssl.SSLSocket
    buffer: bytes
    authenticated: bool
    connectionType: Optional[str]
    protocolVersion: int

    def __init__(self, server: LocalRelayServer, socket: ssl.SSLSocket):
        self.server = server
        self.socket = socket
        self.buffer = b''
        self.serializer = server.serializer
        self.authenticated = False
        self.id = Client.id + 1
        self.connectionType = None
        self.protocolVersion = 1
        Client.id += 1

    def handleData(self) -> None:
        sock_Data: bytes = b''
        try:
            # 16384 is 2^14 self.socket is a ssl wrapped socket.
            # Perhaps this value was chosen as the largest value that could be received [1] to avoid having to loop
            # until a new line is reached.
            # However, the Python docs [2] say:
            # "For best match with hardware and network realities, the value of bufsize should be a relatively
            # small power of 2, for example, 4096."
            # This should probably be changed in the future.
            # See also transport.py handle_server_data in class TCPTransport.
            # [1] https://stackoverflow.com/a/24870153/
            # [2] https://docs.python.org/3.7/library/socket.html#socket.socket.recv
            buffSize = 16384
            sock_Data = self.socket.recv(buffSize)
        except:
            self.close()
            return
        if not sock_Data:  # Disconnect
            self.close()
            return
        data = self.buffer + sock_Data
        if b'\n' not in data:
            self.buffer = data
            return
        self.buffer = b""
        while b'\n' in data:
            line, sep, data = data.partition(b'\n')
            try:
                self.parse(line)
            except ValueError:
                logger.exception('Error parsing line')
                self.close()
                return
        self.buffer += data

    def parse(self, line: bytes) -> None:
        parsed = self.serializer.deserialize(line)
        if 'type' not in parsed:
            return
        if self.authenticated:
            self.send_to_others(**parsed)
            return
        fn = 'do_'+parsed['type']
        if hasattr(self, fn):
            getattr(self, fn)(parsed)

    def asDict(self) -> Dict[str, Any]:
        return dict(id=self.id, connection_type=self.connectionType)

    def do_join(self, obj: Dict[str, Any]) -> None:
        password = obj.get('channel', None)
        if password != self.server.password:
            self.send(type='error', message='incorrect_password')
            self.close()
            return
        self.connectionType = obj.get('connection_type')
        self.authenticated = True
        clients = []
        client_ids = []
        for c in list(self.server.clients.values()):
            if c is self or not c.authenticated:
                continue
            clients.append(c.asDict())
            client_ids.append(c.id)
        self.send(type='channel_joined', channel=self.server.password,
                  user_ids=client_ids, clients=clients)
        self.send_to_others(type='client_joined',
                            user_id=self.id, client=self.asDict())

    def do_protocol_version(self, obj: Dict[str, Any]) -> None:
        version = obj.get('version')
        if not version:
            return
        self.protocolVersion = version

    def close(self) -> None:
        self.socket.close()
        self.server.clientDisconnected(self)

    def send(self, type: str, origin: Optional[int] = None,
             clients: Optional[List[Dict[str, Any]]] = None,
             client: Optional[Dict[str, Any]] = None,
             **kwargs: Any) -> None:
        msg = kwargs
        if self.protocolVersion > 1:
            if origin:
                msg['origin'] = origin
            if clients:
                msg['clients'] = clients
            if client:
                msg['client'] = client
        try:
            data = self.serializer.serialize(type=type, **msg)
            self.socket.sendall(data)
        except:
            self.close()

    def send_to_others(self, origin: Optional[int] = None, **obj: Any) -> None:
        if origin is None:
            origin = self.id
        for c in self.server.clients.values():
            if c is not self and c.authenticated:
                c.send(origin=origin, **obj)
