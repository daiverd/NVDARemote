
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import addonHandler
import braille
import gui
import speech
import ui
import versionInfo
from logHandler import log

from . import (configuration, connection_info, cues, extensionMapper,
               local_machine)
from .protocol import RemoteMessageType
from .transport import RelayTransport, TransportEvents

addonHandler.initTranslation()


EXCLUDED_SPEECH_COMMANDS = (
	speech.commands.BaseCallbackCommand,
	# _CancellableSpeechCommands are not designed to be reported and are used internally by NVDA. (#230)
	speech.commands._CancellableSpeechCommand,
)



class RemoteSession:
	"""Base class for a session that runs on either the master or slave machine."""

	transport: RelayTransport
	localMachine: local_machine.LocalMachine
	mode: Optional[str] = None
	extensionMapper: Optional[extensionMapper.RemoteExtensionMapper]

	def __init__(self, local_machine: local_machine.LocalMachine, transport: RelayTransport) -> None:
		self.localMachine = local_machine
		self.extensionMapper = None
		self.transport = transport
		self.transport.callback_manager.registerCallback(
			'msg_version_mismatch', self.handleVersionMismatch)
		self.transport.callback_manager.registerCallback(
			'msg_motd', self.handleMotd)

	def handleVersionMismatch(self, **kwargs: Any) -> None:
		# translators: Message for version mismatch
		message = _("""The version of the relay server which you have connected to is not compatible with this version of the Remote Client.
Please either use a different server or upgrade your version of the addon.""")
		ui.message(message)
		self.transport.close()

	def handleMotd(self, motd: str, force_display=False, **kwargs):
		if force_display or self.shouldDisplayMotd(motd):
			gui.messageBox(parent=gui.mainFrame, caption=_(
				"Message of the Day"), message=motd)

	def shouldDisplayMotd(self, motd: str) -> bool:
		conf = configuration.get_config()
		host, port = self.transport.address
		host = host.lower()
		address = '{host}:{port}'.format(host=host, port=port)
		motdBytes = motd.encode('utf-8', errors='surrogatepass')
		hashed = hashlib.sha1(motdBytes).hexdigest()
		current = conf['seen_motds'].get(address, "")
		if current == hashed:
			return False
		conf['seen_motds'][address] = hashed
		conf.write()
		return True

	def getConnectionInfo(self) -> connection_info.ConnectionInfo:
		hostname, port = self.transport.address
		key = self.transport.channel
		return connection_info.ConnectionInfo(hostname=hostname, port=port, key=key, mode=self.mode)


class SlaveSession(RemoteSession):
	"""Session that runs on the slave and manages state."""

	mode: str = 'slave'
	extensionMapper: extensionMapper.SlaveExtensionMapper
	masters: Dict[int, Dict[str, Any]]
	masterDisplaySizes: List[int]
	extensionsRegistered: bool

	def __init__(self, *args: Any, **kwargs: Any) -> None:
		super().__init__(*args, **kwargs)
		self.transport.callback_manager.registerCallback(
			'msg_client_joined', self.handleClientConnected)
		self.transport.callback_manager.registerCallback(
			'msg_client_left', self.handleClientDisconnected)
		self.transport.callback_manager.registerCallback(
			'msg_key', self.localMachine.sendKey)
		self.masters = defaultdict(dict)
		self.masterDisplaySizes = []
		self.transport.callback_manager.registerCallback(
			'msg_index', self.recvIndex)
		self.transport.callback_manager.registerCallback(
			TransportEvents.CLOSING, self.handleTransportClosing)
		self.extensionMapper = extensionMapper.SlaveExtensionMapper(self.transport)
		self.extensionsRegistered = False
		self.transport.callback_manager.registerCallback(
			'msg_channel_joined', self.handleChannelJoined)
		self.transport.callback_manager.registerCallback(
			'msg_set_clipboard_text', self.localMachine.setClipboardText)
		self.transport.callback_manager.registerCallback(
			'msg_set_braille_info', self.handleBrailleInfo)
		self.transport.callback_manager.registerCallback(
			'msg_set_display_size', self.setDisplaySize)
		braille.filter_displaySize.register(
			self.localMachine.handleFilterDisplaySize)
		self.transport.callback_manager.registerCallback(
			'msg_braille_input', self.localMachine.brailleInput)
		self.transport.callback_manager.registerCallback(
			'msg_send_SAS', self.localMachine.sendSAS)

	def handleClientConnected(self, client: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
		self.extensionMapper.registerExtensionPoints()
		if not self.extensionsRegistered:
			self.addMapperCallbacks()
			self.extensionsRegistered = True
		cues.client_connected()
		if client['connection_type'] == 'master':
			self.masters[client['id']]['active'] = True

	def handleChannelJoined(self, channel: Optional[str] = None, clients: Optional[List[Dict[str, Any]]] = None, origin: Optional[int] = None, **kwargs: Any) -> None:
		if clients is None:
			clients = []
		for client in clients:
			self.handleClientConnected(client)

	def handleTransportClosing(self) -> None:
		self.extensionMapper.unregisterExtensionPoints()
		if self.extensionsRegistered:
			self.removeMapperCallbacks()
			self.extensionsRegistered = False

	def handleTransportDisconnected(self):
		cues.client_connected()
		self.extensionMapper.unregisterExtensionPoints()

	def handleClientDisconnected(self, client=None, **kwargs):
		cues.client_disconnected()
		if client['connection_type'] == 'master':
			del self.masters[client['id']]
		if not self.masters:
			self.extensionMapper.unregisterExtensionPoints()

	def setDisplaySize(self, sizes=None, **kwargs):
		self.masterDisplaySizes = sizes if sizes else [
			info.get("braille_numCells", 0) for info in self.masters.values()]
		self.localMachine.setBrailleDisplay_size(self.masterDisplaySizes)

	def handleBrailleInfo(self, name: Optional[str] = None, numCells: int = 0, origin: Optional[int] = None, **kwargs: Any) -> None:
		if not self.masters.get(origin):
			return
		self.masters[origin]['braille_name'] = name
		self.masters[origin]['braille_numCells'] = numCells
		self.setDisplaySize()

	def _getMapperCallbacks(self) -> List[Tuple[str, Callable[..., Any]]]:
		return (
			('speak', self.speak),
			('pause_speech', self.pauseSpeech),
			('display', self.display),
			('set_display', self.setDisplaySize)
		)

	def addMapperCallbacks(self) -> None:
		mapperCallbacks = self._getMapperCallbacks()
		for event, callback in mapperCallbacks:
			self.extensionMapper.registerCallback(event, callback)

	def removeMapperCallbacks(self):
		mapperCallbacks = self._getMapperCallbacks()
		for event, callback in mapperCallbacks:
			self.extensionMapper.unregisterCallback(event, callback)

	def _filterUnsupportedSpeechCommands(self, speechSequence: List[Any]) -> List[Any]:
		return list([
			item for item in speechSequence
			if not isinstance(item, EXCLUDED_SPEECH_COMMANDS)
		])

	def speak(self, speechSequence: List[Any], priority: Optional[str]) -> None:
		self.transport.send(
			type=RemoteMessageType.speak,
			sequence=self._filterUnsupportedSpeechCommands(speechSequence),
			priority=priority
		)

	def pauseSpeech(self, switch):
		self.transport.send(type=RemoteMessageType.pause_speech, switch=switch)

	def display(self, cells):
		# Only send braille data when there are controlling machines with a braille display
		if self.hasBrailleMasters():
			self.transport.send(type=RemoteMessageType.display, cells=cells)

	def hasBrailleMasters(self):
		return bool([i for i in self.masterDisplaySizes if i > 0])

	def recvIndex(self, index=None, **kwargs):
		pass  # speech index approach changed in 2019.3


class MasterSession(RemoteSession):

	mode: str = 'master'
	extensionMapper: extensionMapper.MasterExtensionMapper
	slaves: Dict[int, Dict[str, Any]]
	extensionsRegistered: bool

	def __init__(self, *args: Any, **kwargs: Any) -> None:
		super().__init__(*args, **kwargs)
		self.slaves = defaultdict(dict)
		self.extensionMapper = extensionMapper.MasterExtensionMapper(transport=self.transport)
		self.extensionsRegistered = False
		self.transport.callback_manager.registerCallback(
			'msg_speak', self.localMachine.speak)
		self.transport.callback_manager.registerCallback(
			'msg_cancel', self.localMachine.cancelSpeech)
		self.transport.callback_manager.registerCallback(
			'msg_pause_speech', self.localMachine.pauseSpeech)
		self.transport.callback_manager.registerCallback(
			'msg_tone', self.localMachine.beep)
		self.transport.callback_manager.registerCallback(
			'msg_wave', self.handlePlayWave)
		self.transport.callback_manager.registerCallback(
			'msg_display', self.localMachine.display)
		self.transport.callback_manager.registerCallback(
			'msg_nvda_not_connected', self.handleNVDANotConnected)
		self.transport.callback_manager.registerCallback(
			'msg_client_joined', self.handleClientConnected)
		self.transport.callback_manager.registerCallback(
			'msg_client_left', self.handleClientDisconnected)
		self.transport.callback_manager.registerCallback(
			'msg_channel_joined', self.handleChannel_joined)
		self.transport.callback_manager.registerCallback(
			'msg_set_clipboard_text', self.localMachine.setClipboardText)
		self.transport.callback_manager.registerCallback(
			'msg_send_braille_info', self.sendBrailleInfo)
		self.transport.callback_manager.registerCallback(
			TransportEvents.CONNECTED, self.handleConnected)
		self.transport.callback_manager.registerCallback(
			TransportEvents.DISCONNECTED, self.handleDisconnected)

	def handlePlayWave(self, **kwargs):
		"""Receive instruction to play a 'wave' from the slave machine
		This method handles translation (between versions of NVDA Remote) of arguments required for 'msg_wave'
		"""
		# Note:
		# Version 2.2 will send only 'async' in kwargs
		# Version 2.3 will send 'asynchronous' and 'async' in kwargs
		if "fileName" not in kwargs:
			log.error("'fileName' missing from kwargs.")
			return
		fileName = kwargs.pop("fileName")
		self.localMachine.playWave(fileName=fileName)

	def handleNVDANotConnected(self) -> None:
		speech.cancelSpeech()
		ui.message(_("Remote NVDA not connected."))

	def handleConnected(self):
		# speech index approach changed in 2019.3
		pass  # nothing to do

	def handleDisconnected(self):
		# speech index approach changed in 2019.3
		pass  # nothing to do

	def handleChannel_joined(self, channel: Optional[str] = None, clients: Optional[List[Dict[str, Any]]] = None, origin: Optional[int] = None, **kwargs: Any) -> None:
		if clients is None:
			clients = []
		for client in clients:
			self.handleClientConnected(client)

	def handleClientConnected(self, client=None, **kwargs):
		self.extensionMapper.registerExtensionPoints()
		if not self.extensionsRegistered:
			self.addMapperCallbacks()
			self.extensionsRegistered = True
		self.sendBrailleInfo()
		cues.client_connected()

	def handleClientDisconnected(self, client=None, **kwargs):
		self.extensionMapper.unregisterExtensionPoints()
		if self.extensionsRegistered:
			self.removeMapperCallbacks()
			self.extensionsRegistered = False
		cues.client_disconnected()

	def sendBrailleInfo(self, display: Optional[Any] = None, displaySize: Optional[int] = None, **kwargs: Any) -> None:
		if display is None:
			display = braille.handler.display
		if displaySize is None:
			displaySize = braille.handler.displaySize
		self.transport.send(type=RemoteMessageType.set_braille_info,
							name=display.name, numCells=displaySize)

	def brailleInput(self, **kwargs: Any) -> None:
		self.transport.send(type=RemoteMessageType.braille_input, **kwargs)

	def addMapperCallbacks(self):
		patcher_callbacks = (('braille_input', self.brailleInput), ('set_display', self.sendBrailleInfo))
		for event, callback in patcher_callbacks:
			self.extensionMapper.registerCallback(event, callback)

	def removeMapperCallbacks(self):
		mapperCallbacks = (('braille_input', self.brailleInput),
							 ('set_display', self.sendBrailleInfo))
		for event, callback in mapperCallbacks:
			self.extensionMapper.unregisterCallback(event, callback)
