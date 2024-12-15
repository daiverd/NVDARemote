"""Network transport layer for NVDA Remote.

This module provides the core networking functionality for NVDA Remote.

Classes:
    Transport: Base class defining the transport interface
    TCPTransport: Implementation of secure TCP socket transport
    RelayTransport: Extended TCP transport for relay server connections
    ConnectorThread: Helper class for connection management

The transport layer handles:
    * Secure socket connections with SSL/TLS
    * Message serialization and deserialization
    * Connection management and reconnection
    * Event notifications for connection state changes
    * Message routing based on RemoteMessageType enum

All network operations run in background threads, while message handlers
are called on the main wxPython thread for thread-safety.
"""

import hashlib
import select
import socket
import ssl
import threading
import time
from enum import Enum
from logging import getLogger
from queue import Queue
from typing import Any, Callable, Dict, Optional, Tuple, Union

import wx
from extensionPoints import Action

from . import configuration
from .protocol import PROTOCOL_VERSION, RemoteMessageType
from .serializer import Serializer
from .socket_utils import hostPortToAddress

log = getLogger("transport")


class Transport:
    """Base class defining the network transport interface for NVDA Remote.

    This abstract base class defines the interface that all network transports must implement.
    It provides core functionality for secure message passing, connection management,
    and event handling between NVDA instances.

    The Transport class handles:

    * Message serialization and routing using a pluggable serializer
    * Connection state management and event notifications
    * Registration of message type handlers
    * Thread-safe connection events

    To implement a new transport:

    1. Subclass Transport
    2. Implement connection logic in run()
    3. Call onTransportConnected() when connected
    4. Use send() to transmit messages
    5. Call appropriate event notifications

    Example:
        >>> serializer = JSONSerializer()
        >>> transport = TCPTransport(serializer, ("localhost", 8090))
        >>> transport.registerInbound(RemoteMessageType.key, handle_key)
        >>> transport.run()

    Args:
        serializer: The serializer instance to use for message encoding/decoding

    Attributes:
        connected: True if transport has an active connection
        successful_connects: Counter of successful connection attempts
        connected_event: Event that is set when connected
        serializer: The message serializer instance
        inboundHandlers: Registered message handlers

    Events:
        transportConnected: Fired after connection is established and ready
        transportDisconnected: Fired when existing connection is lost
        transportCertificateAuthenticationFailed: Fired when SSL certificate validation fails
        transportConnectionFailed: Fired when a connection attempt fails
        transportClosing: Fired before transport is shut down
    """

    # Whether transport has an active connection
    connected: bool
    # Counter of successful connection attempts
    successfulConnects: int
    # Event that is set when connected, cleared when disconnected
    connectedEvent: threading.Event
    # Message serializer instance for encoding/decoding network messages
    serializer: Serializer

    def __init__(self, serializer: Serializer) -> None:
        """Initialize transport with message serializer."""
        # Handles message encoding/decoding for network transmission
        self.serializer = serializer
        self.connected = False
        self.successfulConnects = 0
        self.connectedEvent = threading.Event()
        # iterate over all the message types and create a dictionary of handlers mapping to Action()
        self.inboundHandlers: Dict[RemoteMessageType, Callable] = {
            msg: Action() for msg in RemoteMessageType
        }
        self.transportConnected = Action()
        """
		Notifies when the transport is connected
		"""
        self.transportDisconnected = Action()
        """
		Notifies when the transport is disconnected
		"""
        self.transportCertificateAuthenticationFailed = Action()
        """
		Notifies when the transport fails to authenticate the certificate
		"""
        self.transportConnectionFailed = Action()
        """
		Notifies when the transport fails to connect
		"""
        self.transportClosing = Action()
        """
		Notifies when the transport is closing
		"""

    def onTransportConnected(self) -> None:
        """Handle successful transport connection.

        Called internally when a connection is established. Updates connection state,
        increments successful connection counter, and notifies listeners.

        State changes:
            - Increments successfulConnects counter
            - Sets connected flag to True 
            - Sets connectedEvent threading.Event
            - Notifies all transportConnected listeners

        Threading:
            - Can be called from any thread
            - Thread-safe due to atomic operations
            - Listeners are called on the same thread

        Note:
            This is an internal method called by transport implementations
            after establishing a connection. External code should register
            for transportConnected notifications instead.
        """
        self.successfulConnects += 1
        self.connected = True
        self.connectedEvent.set()
        self.transportConnected.notify()

    def registerInbound(self, type: RemoteMessageType, handler: Callable) -> None:
        """Register a handler for incoming messages of a specific type.

        Adds a callback function to handle messages of the specified type.
        Multiple handlers can be registered for the same message type.
        Handlers are called in registration order when messages arrive.

        Threading:
            - Registration is thread-safe
            - Handlers are called asynchronously on wx main thread
            - Handler execution order is preserved

        Example:
            >>> def handle_keypress(key_code: int, pressed: bool) -> None:
            ...     print(f"Key {key_code} {'pressed' if pressed else 'released'}")
            >>> transport.registerInbound(RemoteMessageType.key_press, handle_keypress)

        Note:
            - Handlers must accept **kwargs for forward compatibility
            - Exceptions in handlers are caught and logged
            - Handlers can be unregistered with unregisterInbound()
            - Same handler can be registered multiple times
        """
        # The message type to handle
        self.type = type
        # Callback function to process messages, called with payload as kwargs
        self.handler = handler
        self.inboundHandlers[type].register(handler)

    def unregisterInbound(self, type: RemoteMessageType, handler: Callable) -> None:
        """Remove a previously registered message handler.

        Removes a specific handler function from the list of handlers for a message type.
        If the handler was not previously registered, this is a no-op.

        Threading:
            - Unregistration is thread-safe
            - Can be called while handlers are executing
            - Removed handler won't receive future messages

        Example:
            >>> transport.unregisterInbound(RemoteMessageType.key_press, handle_keypress)

        Note:
            - Must pass the exact same handler function that was registered
            - Removing a handler multiple times is safe
            - Other handlers for same type are unaffected
            - In-flight messages may still reach the handler
        """
        # The message type to unregister from
        self.type = type
        # The handler function to remove from the registry
        self.handler = handler
        self.inboundHandlers[type].unregister(handler)


class TCPTransport(Transport):
    """Secure TCP socket transport implementation.

    This class implements the Transport interface using TCP sockets with SSL/TLS
    encryption. It handles connection establishment, data transfer, and connection
    lifecycle management.

    Args:
        serializer: Message serializer instance
        address: Remote address to connect to
        timeout: Connection timeout in seconds. Defaults to 0.
        insecure: Skip certificate verification. Defaults to False.

    Attributes:
        buffer: Buffer for incomplete received data
        closed: Whether transport is closed
        queue: Queue of outbound messages
        insecure: Whether to skip certificate verification
        address: Remote address to connect to
        timeout: Connection timeout in seconds
        serverSock: The SSL socket connection
        serverSockLock: Lock for thread-safe socket access
        queueThread: Thread handling outbound messages
        reconnectorThread: Thread managing reconnection
    """

    # Buffer for incomplete received data
    buffer: bytes
    # Whether transport is intentionally closed
    closed: bool
    # Queue of outbound messages pending send
    queue: Queue[Optional[bytes]]
    # Whether to skip SSL certificate verification
    insecure: bool
    # Lock for thread-safe socket access
    serverSockLock: threading.Lock
    # Remote (host, port) to connect to
    address: Tuple[str, int]
    # Active SSL socket connection
    serverSock: Optional[ssl.SSLSocket]
    # Thread handling outbound message queue
    queueThread: Optional[threading.Thread]
    # Connection timeout in seconds
    timeout: int
    # Thread managing reconnection attempts
    reconnectorThread: "ConnectorThread"
    # Last failed certificate fingerprint
    lastFailFingerprint: Optional[str]

    def __init__(
        self,
        serializer: Serializer,
        address: Tuple[str, int],
        timeout: int = 0,
        insecure: bool = False,
    ) -> None:
        super().__init__(serializer=serializer)
        self.closed = False
        # Buffer to hold partially received data
        self.buffer = b""
        self.queue = Queue()
        self.address = address
        self.serverSock = None
        # Reading/writing from an SSL socket is not thread safe.
        # See https://bugs.python.org/issue41597#msg375692
        # Guard access to the socket with a lock.
        self.serverSockLock = threading.Lock()
        self.queueThread = None
        self.timeout = timeout
        self.reconnectorThread = ConnectorThread(self)
        self.insecure = insecure

    def run(self) -> None:
        """Main connection loop for the transport.
        
        Establishes connection, handles certificate verification,
        processes incoming data, and manages reconnection.
        
        Raises:
            ssl.SSLCertVerificationError: If certificate verification fails
            socket.error: For other connection failures
        """
        self.closed = False
        try:
            self.serverSock = self.createOutboundSocket(
                *self.address, insecure=self.insecure
            )
            self.serverSock.connect(self.address)
        except ssl.SSLCertVerificationError:
            fingerprint = None
            try:
                tmp_con = self.createOutboundSocket(*self.address, insecure=True)
                tmp_con.connect(self.address)
                certBin = tmp_con.getpeercert(True)
                tmp_con.close()
                fingerprint = hashlib.sha256(certBin).hexdigest().lower()
            except Exception:
                pass
            config = configuration.get_config()
            if (
                hostPortToAddress(self.address) in config["trusted_certs"]
                and config["trusted_certs"][hostPortToAddress(self.address)]
                == fingerprint
            ):
                self.insecure = True
                return self.run()
            self.lastFailFingerprint = fingerprint
            self.transportCertificateAuthenticationFailed.notify()
            raise
        except Exception:
            self.transportConnectionFailed.notify()
            raise
        self.onTransportConnected()
        self.queueThread = threading.Thread(target=self.sendQueue)
        self.queueThread.daemon = True
        self.queueThread.start()
        while self.serverSock is not None:
            try:
                readers, writers, error = select.select(
                    [self.serverSock], [], [self.serverSock]
                )
            except socket.error:
                self.buffer = b""
                break
            if self.serverSock in error:
                self.buffer = b""
                break
            if self.serverSock in readers:
                try:
                    self.processIncomingSocketData()
                except socket.error:
                    self.buffer = b""
                    break
        self.connected = False
        self.connectedEvent.clear()
        self.transportDisconnected.notify()
        self._disconnect()

    def createOutboundSocket(
        self, host: str, port: int, insecure: bool = False
    ) -> ssl.SSLSocket:
        """Create and configure an SSL socket for outbound connections.

        Creates a TCP socket with appropriate timeout and keep-alive settings,
        then wraps it with SSL/TLS encryption.

        Note:
            The socket is created but not yet connected. Call connect() separately.
        """
        # Remote hostname to connect to
        self.host = host
        # Remote port number
        self.port = port
        # Whether to skip certificate verification
        self.insecure = insecure
        address = socket.getaddrinfo(host, port)[0]
        serverSock = socket.socket(*address[:3])
        if self.timeout:
            serverSock.settimeout(self.timeout)
        serverSock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        serverSock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60000, 2000))
        ctx = ssl.SSLContext()
        if insecure:
            ctx.verify_mode = ssl.CERT_NONE
        ctx.check_hostname = not insecure
        ctx.load_default_certs()
        serverSock = ctx.wrap_socket(sock=serverSock, server_hostname=host)
        return serverSock

    def getpeercert(
        self, binary_form: bool = False
    ) -> Optional[Union[Dict[str, Any], bytes]]:
        """Get the certificate from the peer.

        Retrieves the certificate presented by the remote peer during SSL handshake.
        Returns None if not connected.
        """
        # Whether to return raw certificate bytes instead of parsed dictionary
        self.binary_form = binary_form
        if self.serverSock is None:
            return None
        return self.serverSock.getpeercert(binary_form)

    def processIncomingSocketData(self) -> None:
        """Process incoming socket data in chunks.

        Handles newline-delimited messages with partial buffering.
        Uses non-blocking SSL socket reads with thread safety.
        Empty reads trigger disconnect.
        """
        # This approach may be problematic:
        # See also server.py handle_data in class Client.
        buffSize = 16384
        with self.serverSockLock:
            # select operates on the raw socket. Even though it said there was data to
            # read, that might be SSL data which might not result in actual data for
            # us. Therefore, do a non-blocking read so SSL doesn't try to wait for
            # more data for us.
            # We don't make the socket non-blocking earlier because then we'd have to
            # handle retries during the SSL handshake.
            # See https://stackoverflow.com/questions/3187565/select-and-ssl-in-python
            # and https://docs.python.org/3/library/ssl.html#notes-on-non-blocking-sockets
            self.serverSock.setblocking(False)
            try:
                data = self.buffer + self.serverSock.recv(buffSize)
            except ssl.SSLWantReadError:
                # There's no data for us.
                return
            finally:
                self.serverSock.setblocking(True)
        self.buffer = b""
        if not data:
            self._disconnect()
            return
        if b"\n" not in data:
            self.buffer += data
            return
        while b"\n" in data:
            line, sep, data = data.partition(b"\n")
            self.parse(line)
        self.buffer += data

    def parse(self, line: bytes) -> None:
        """Parse and handle a complete message line.

        Deserializes JSON message and routes to appropriate handler.
        Messages must have a 'type' field identifying the message type.
        Remaining fields are passed as kwargs to the handler.

        Handler callbacks run asynchronously on wx main thread.
        Invalid messages are logged and ignored.
        """
        obj = self.serializer.deserialize(line)
        if "type" not in obj:
            log.error("Received message without type: %r" % obj)
            return
        try:
            messageType = RemoteMessageType(obj["type"])
        except ValueError:
            log.error("Received message with invalid type: %r" % obj)
            return
        del obj["type"]
        extensionPoint = self.inboundHandlers.get(messageType)
        if not extensionPoint:
            log.error("Received message with unhandled type: %r" % obj)
            return
        wx.CallAfter(extensionPoint.notify, **obj)

    def sendQueue(self) -> None:
        """Process outbound message queue in background thread.

        Continuously sends queued messages over socket.
        Thread exits on None message or socket error.
        Uses lock for thread-safe socket access.
        """
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                with self.serverSockLock:
                    self.serverSock.sendall(item)
            except socket.error:
                return

    def send(self, type: str | Enum, **kwargs: Any) -> None:
        """Queue a message for asynchronous transmission.
        
        Thread-safe method to send messages.
        Drops messages if transport not connected.
        """
        if self.connected:
            obj = self.serializer.serialize(type=type, **kwargs)
            self.queue.put(obj)
        else:
            log.error("Attempted to send message %r while not connected", type)

    def _disconnect(self) -> None:
        """Internal method to handle transport disconnection.
        
        Cleans up queue thread and socket on errors.
        Unlike close(), does not shut down connector thread.
        """
        if self.queueThread is not None:
            self.queue.put(None)
            self.queueThread.join()
            self.queueThread = None
        clearQueue(self.queue)
        if self.serverSock:
            self.serverSock.close()
            self.serverSock = None

    def close(self) -> None:
        """Perform orderly shutdown of the transport.
        
        Notifies listeners, stops reconnection, and cleans up resources.
        Use this method rather than _disconnect() for proper shutdown.
        """
        self.transportClosing.notify()
        self.reconnectorThread.running = False
        self._disconnect()
        self.closed = True
        self.reconnectorThread = ConnectorThread(self)


class RelayTransport(TCPTransport):
    """Transport for connecting through a relay server.

    Extends TCPTransport with relay-specific protocol handling for channels
    and connection types. Manages protocol versioning and channel joining.

    Args:
        serializer: Message serializer instance
        address: Relay server address
        timeout: Connection timeout. Defaults to 0.
        channel: Channel to join. Defaults to None.
        connectionType: Connection type. Defaults to None.
        protocol_version: Protocol version. Defaults to PROTOCOL_VERSION.
        insecure: Skip certificate verification. Defaults to False.

    Attributes:
        channel: Relay channel name
        connectionType: Type of relay connection
        protocol_version: Protocol version to use
    """

    # Relay channel name to join
    channel: Optional[str]
    # Type of relay connection (master/slave)
    connectionType: Optional[str]
    # Protocol version for compatibility
    protocol_version: int

    def __init__(
        self,
        serializer: Serializer,
        address: Tuple[str, int],
        timeout: int = 0,
        channel: Optional[str] = None,
        connectionType: Optional[str] = None,
        protocol_version: int = PROTOCOL_VERSION,
        insecure: bool = False,
    ) -> None:
        super().__init__(
            address=address, serializer=serializer, timeout=timeout, insecure=insecure
        )
        log.info("Connecting to %s channel %s" % (address, channel))
        self.channel = channel
        self.connectionType = connectionType
        self.protocol_version = protocol_version
        self.transportConnected.register(self.onConnected)

    def onConnected(self) -> None:
        """Handle relay server connection and protocol handshake.
        
        Sends version and either joins channel or requests new key.
        """
        self.send(RemoteMessageType.protocol_version, version=self.protocol_version)
        if self.channel is not None:
            self.send(
                RemoteMessageType.join,
                channel=self.channel,
                connection_type=self.connectionType,
            )
        else:
            self.send(RemoteMessageType.generate_key)


class ConnectorThread(threading.Thread):
    """Background thread that manages connection attempts.

    Handles automatic reconnection with configurable delay between attempts.
    Runs until explicitly stopped.

    Args:
        connector: Transport instance to manage connections for
        reconnectDelay: Seconds between attempts. Defaults to 5.

    Attributes:
        running: Whether thread should continue running
        connector: Transport to manage connections for
        reconnectDelay: Seconds to wait between connection attempts
    """

    running: bool
    connector: Transport
    reconnectDelay: int

    def __init__(self, connector: Transport, reconnectDelay: int = 5) -> None:
        super().__init__()
        self.reconnectDelay = reconnectDelay
        self.running = True
        self.connector = connector
        self.name = self.name + "_connector_loop"
        self.daemon = True

    def run(self):
        while self.running:
            try:
                self.connector.run()
            except socket.error:
                time.sleep(self.reconnectDelay)
                continue
            else:
                time.sleep(self.reconnectDelay)
        log.info("Ending control connector thread %s" % self.name)


def clearQueue(queue: Queue[Optional[bytes]]) -> None:
    """Empty queue without blocking to prevent memory leaks.
    
    Non-blocking removal of all pending items.
    Used during shutdown and error recovery.
    """
    try:
        while True:
            queue.get_nowait()
    except Exception:
        pass
