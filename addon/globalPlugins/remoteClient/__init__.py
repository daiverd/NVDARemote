import logging
logger = logging.getLogger(__name__)

import ctypes
import ctypes.wintypes
import os
import sys
import json
import threading
import socket
import ssl
import uuid

import configobj
import wx


import addonHandler
import api
from globalPluginHandler import GlobalPlugin as _GlobalPlugin
from keyboardHandler import KeyboardInputGesture
from scriptHandler import script
import IAccessibleHandler
import globalVars

import ui
from config import isInstalledCopy
import gui
import speech
import braille
from . import local_machine
from . import serializer
from . import configuration
from . import cues
from .transport import RelayTransport, TransportEvents
from .session import MasterSession, SlaveSession
from . import url_handler
try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	from logHandler import log
	log.warning(
		"Unable to initialise translations. This may be because the addon is running from NVDA scratchpad."
	)
from . import dialogs
from . import keyboard_hook
from . import server
from . import bridge
from .socket_utils import SERVER_PORT, address_to_hostport, hostport_to_address

from winUser import WM_QUIT  # provided by NVDA
logging.getLogger("keyboard_hook").addHandler(logging.StreamHandler(sys.stdout))
from logHandler import log

import shlobj


import queueHandler
import versionInfo
if versionInfo.version_year >= 2024:
	from winAPI.secureDesktop import post_secureDesktopStateChange


class GlobalPlugin(_GlobalPlugin):
	scriptCategory = _("NVDA Remote")
	localScripts = set()

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.keyModifiers = set()
		self.hostPendingModifiers = set()
		self.localScripts = {self.script_sendKeys}
		self.localMachine = local_machine.LocalMachine()
		self.slave_session = None
		self.master_session = None
		self.createMenu()
		self.connecting = False
		self.url_handler_window = url_handler.URLHandlerWindow(callback=self.verify_connect)
		url_handler.register_url_handler()
		self.master_transport = None
		self.slave_transport = None
		self.server = None
		self.hook_thread = None
		self.sending_keys = False
		self.key_modified = False
		self.sd_server = None
		self.sd_relay = None
		self.sd_bridge = None
		try:
			configuration.get_config()
		except configobj.ParseError:
			os.remove(os.path.abspath(os.path.join(globalVars.appArgs.configPath, configuration.CONFIG_FILE_NAME)))
			queueHandler.queueFunction(queueHandler.eventQueue, wx.CallAfter, wx.MessageBox, _("Your NVDA Remote configuration was corrupted and has been reset."), _("NVDA Remote Configuration Error"), wx.OK|wx.ICON_EXCLAMATION)
		cs = configuration.get_config()['controlserver']
		if hasattr(shlobj, 'SHGetKnownFolderPath'):
			self.temp_location = os.path.join(shlobj.SHGetKnownFolderPath(shlobj.FolderId.PROGRAM_DATA), 'temp')
		else:
			self.temp_location = os.path.join(shlobj.SHGetFolderPath(0, shlobj.CSIDL_COMMON_APPDATA), 'temp')
		self.ipc_file = os.path.join(self.temp_location, 'remote.ipc')
		if globalVars.appArgs.secure:
			self.handle_secure_desktop()
		if cs['autoconnect'] and not self.master_session and not self.slave_session:
			self.performAutoconnect()
		self.sd_focused = False
		if versionInfo.version_year >= 2024:
			post_secureDesktopStateChange.register(self.onSecureDesktopChange)

	def performAutoconnect(self):
		controlServerConfig = configuration.get_config()['controlserver']
		channel = controlServerConfig['key']
		if controlServerConfig['self_hosted']:
			port = controlServerConfig['port']
			address = ('localhost',port)
			self.start_control_server(port, channel)
		else:
			address = address_to_hostport(controlServerConfig['host'])
		if controlServerConfig['connection_type']==0:
			self.connect_AsSlave(address, channel)
		else:
			self.connectAsMaster(address, channel)

	def createMenu(self):
		self.menu = wx.Menu()
		tools_menu = gui.mainFrame.sysTrayIcon.toolsMenu
		# Translators: Item in NVDA Remote submenu to connect to a remote computer.
		self.connect_item = self.menu.Append(wx.ID_ANY, _("Connect..."), _("Remotely connect to another computer running NVDA Remote Access"))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.do_connect, self.connect_item)
		# Translators: Item in NVDA Remote submenu to disconnect from a remote computer.
		self.disconnect_item = self.menu.Append(wx.ID_ANY, _("Disconnect"), _("Disconnect from another computer running NVDA Remote Access"))
		self.disconnect_item.Enable(False)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_disconnect_item, self.disconnect_item)
		# Translators: Menu item in NvDA Remote submenu to mute speech and sounds from the remote computer.
		self.mute_item = self.menu.Append(wx.ID_ANY, _("Mute remote"), _("Mute speech and sounds from the remote computer"), kind=wx.ITEM_CHECK)
		self.mute_item.Enable(False)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_mute_item, self.mute_item)
		# Translators: Menu item in NVDA Remote submenu to push clipboard content to the remote computer.
		self.push_clipboard_item = self.menu.Append(wx.ID_ANY, _("&Push clipboard"), _("Push the clipboard to the other machine"))
		self.push_clipboard_item.Enable(False)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_push_clipboard_item, self.push_clipboard_item)
		# Translators: Menu item in NVDA Remote submenu to copy a link to the current session.
		self.copy_link_item = self.menu.Append(wx.ID_ANY, _("Copy &link"), _("Copy a link to the remote session"))
		self.copy_link_item.Enable(False)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_copy_link_item, self.copy_link_item)
		# Translators: Menu item in NvDA Remote submenu to open add-on options.
		self.options_item = self.menu.Append(wx.ID_ANY, _("&Options..."), _("Options"))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_options_item, self.options_item)
		# Translators: Menu item in NVDA Remote submenu to send Control+Alt+Delete to the remote computer.
		self.send_ctrl_alt_del_item = self.menu.Append(wx.ID_ANY, _("Send Ctrl+Alt+Del"), _("Send Ctrl+Alt+Del"))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.on_send_ctrl_alt_del, self.send_ctrl_alt_del_item)
		self.send_ctrl_alt_del_item.Enable(False)
		# Translators: Label of menu in NVDA tools menu.
		self.remote_item=tools_menu.AppendSubMenu(self.menu, _("R&emote"), _("NVDA Remote Access"))

	def terminate(self):
		if versionInfo.version_year >= 2024:
			post_secureDesktopStateChange.unregister(self.onSecureDesktopChange)
		self.disconnect()
		self.localMachine.terminate()
		self.localMachine = None
		self.menu.Remove(self.connect_item.Id)
		self.connect_item.Destroy()
		self.connect_item=None
		self.menu.Remove(self.disconnect_item.Id)
		self.disconnect_item.Destroy()
		self.disconnect_item=None
		self.menu.Remove(self.mute_item.Id)
		self.mute_item.Destroy()
		self.mute_item=None
		self.menu.Remove(self.push_clipboard_item.Id)
		self.push_clipboard_item.Destroy()
		self.push_clipboard_item=None
		self.menu.Remove(self.copy_link_item.Id)
		self.copy_link_item.Destroy()
		self.copy_link_item = None
		self.menu.Remove(self.options_item.Id)
		self.options_item.Destroy()
		self.options_item=None
		self.menu.Remove(self.send_ctrl_alt_del_item.Id)
		self.send_ctrl_alt_del_item.Destroy()
		self.send_ctrl_alt_del_item=None
		tools_menu = gui.mainFrame.sysTrayIcon.toolsMenu
		tools_menu.Remove(self.remote_item.Id)
		self.remote_item.Destroy()
		self.remote_item=None
		try:
			self.menu.Destroy()
		except (RuntimeError, AttributeError):
			pass
		try:
			os.unlink(self.ipc_file)
		except:
			pass
		self.menu=None
		if not isInstalledCopy():
			url_handler.unregister_url_handler()
		self.url_handler_window.destroy()
		self.url_handler_window=None

	def on_disconnect_item(self, evt):
		evt.Skip()
		self.disconnect()

	def on_mute_item(self, evt):
		evt.Skip()
		self.localMachine.is_muted = self.mute_item.IsChecked()

	def script_toggle_remote_mute(self, gesture):
		if not self.is_connected() or self.connecting: return
		self.localMachine.is_muted = not self.localMachine.is_muted
		self.mute_item.Check(self.localMachine.is_muted)
		# Translators: Report when using gestures to mute or unmute the speech coming from the remote computer.
		status = _("Mute speech and sounds from the remote computer") if self.localMachine.is_muted else _("Unmute speech and sounds from the remote computer")
		ui.message(status)
	script_toggle_remote_mute.__doc__ = _("""Mute or unmute the speech coming from the remote computer""")

	def on_push_clipboard_item(self, evt):
		connector = self.slave_transport or self.master_transport
		try:
			connector.send(type='set_clipboard_text', text=api.getClipData())
			cues.clipboard_pushed()
		except TypeError:
			log.exception("Unable to push clipboard")

	def script_push_clipboard(self, gesture):
		connector = self.slave_transport or self.master_transport
		if not getattr(connector,'connected',False):
			ui.message(_("Not connected."))
			return
		try:
			connector.send(type='set_clipboard_text', text=api.getClipData())
			cues.clipboard_pushed()
			ui.message(_("Clipboard pushed"))
		except TypeError:
			ui.message(_("Unable to push clipboard"))
	script_push_clipboard.__doc__ = _("Sends the contents of the clipboard to the remote machine")

	def on_copy_link_item(self, evt):
		session = self.master_session or self.slave_session
		url = session.get_connection_info().get_url_to_connect()
		api.copyToClip(str(url))

	def script_copy_link(self, gesture):
		self.on_copy_link_item(None)
		ui.message(_("Copied link"))
	script_copy_link.__doc__ = _("Copies a link to the remote session to the clipboard")

	def on_options_item(self, evt):
		evt.Skip()
		conf = configuration.get_config()
		# Translators: The title of the add-on options dialog.
		dlg = dialogs.OptionsDialog(gui.mainFrame, wx.ID_ANY, title=_("Options"))
		dlg.set_from_config(conf)
		def handle_dlg_complete(dlg_result):
			if dlg_result != wx.ID_OK:
				return
			dlg.write_to_config(conf)
		gui.runScriptModalDialog(dlg, callback=handle_dlg_complete)

	def on_send_ctrl_alt_del(self, evt):
		self.master_transport.send('send_SAS')

	def disconnect(self):
		if self.master_transport is None and self.slave_transport is None:
			return
		if self.server is not None:
			self.server.close()
			self.server = None
		if self.master_transport is not None:
			self.disconnect_as_master()
		if self.slave_transport is not None:
			self.disconnect_as_slave()
		cues.disconnected()
		self.disconnect_item.Enable(False)
		self.connect_item.Enable(True)
		self.push_clipboard_item.Enable(False)
		self.copy_link_item.Enable(False)

	def disconnect_as_master(self):
		self.master_transport.close()
		self.master_transport = None
		self.master_session = None

	def disconnecting_as_master(self):
		if self.menu:
			self.connect_item.Enable(True)
			self.disconnect_item.Enable(False)
			self.mute_item.Check(False)
			self.mute_item.Enable(False)
			self.push_clipboard_item.Enable(False)
			self.copy_link_item.Enable(False)
			self.send_ctrl_alt_del_item.Enable(False)
		if self.localMachine:
			self.localMachine.is_muted = False
		self.sending_keys = False
		if self.hook_thread is not None:
			ctypes.windll.user32.PostThreadMessageW(self.hook_thread.ident, WM_QUIT, 0, 0)
			self.hook_thread.join()
			self.hook_thread = None
		self.key_modified = False
		self.keyModifiers = set()

	def disconnect_as_slave(self):
		self.slave_transport.close()
		self.slave_transport = None
		self.slave_session = None

	def on_connected_as_master_failed(self):
		if self.master_transport.successful_connects == 0:
			self.disconnect_as_master()
			# Translators: Title of the connection error dialog.
			gui.messageBox(parent=gui.mainFrame, caption=_("Error Connecting"),
			# Translators: Message shown when cannot connect to the remote computer.
			message=_("Unable to connect to the remote computer"), style=wx.OK | wx.ICON_WARNING)

	def script_disconnect(self, gesture):
		if self.master_transport is None and self.slave_transport is None:
			ui.message(_("Not connected."))
			return
		self.disconnect()
	script_disconnect.__doc__ = _("""Disconnect a remote session""")

	def script_connect(self, gesture):
		if self.is_connected() or self.connecting: return
		self.do_connect(evt = None)
	script_connect.__doc__ = _("""Connect to a remote computer""")
	
	def do_connect(self, evt):
		if evt is not None: evt.Skip()
		last_cons = configuration.get_config()['connections']['last_connected']
		# Translators: Title of the connect dialog.
		dlg = dialogs.DirectConnectDialog(parent=gui.mainFrame, id=wx.ID_ANY, title=_("Connect"))
		dlg.panel.host.SetItems(list(reversed(last_cons)))
		dlg.panel.host.SetSelection(0)
		def handle_dlg_complete(dlg_result):
			if dlg_result != wx.ID_OK:
				return
			if dlg.client_or_server.GetSelection() == 0: #client
				host = dlg.panel.host.GetValue()
				server_addr, port = address_to_hostport(host)
				channel = dlg.panel.key.GetValue()
				if dlg.connection_type.GetSelection() == 0:
					self.connectAsMaster((server_addr, port), channel)
				else:
					self.connect_AsSlave((server_addr, port), channel)
			else: #We want a server
				channel = dlg.panel.key.GetValue()
				self.start_control_server(int(dlg.panel.port.GetValue()), channel)
				if dlg.connection_type.GetSelection() == 0:
					self.connectAsMaster(('127.0.0.1', int(dlg.panel.port.GetValue())), channel, insecure=True)
				else:
					self.connect_AsSlave(('127.0.0.1', int(dlg.panel.port.GetValue())), channel, insecure=True)
		gui.runScriptModalDialog(dlg, callback=handle_dlg_complete)

	def on_connected_as_master(self):
		configuration.write_connection_to_config(self.master_transport.address)
		self.disconnect_item.Enable(True)
		self.connect_item.Enable(False)
		self.mute_item.Enable(True)
		self.push_clipboard_item.Enable(True)
		self.copy_link_item.Enable(True)
		self.send_ctrl_alt_del_item.Enable(True)
		# We might have already created a hook thread before if we're restoring an
		# interrupted connection. We must not create another.
		if not self.hook_thread:
			self.hook_thread = threading.Thread(target=self.hook)
			self.hook_thread.daemon = True
			self.hook_thread.start()
		# Translators: Presented when connected to the remote computer.
		ui.message(_("Connected!"))
		cues.connected()

	def on_disconnected_as_master(self):
		# Translators: Presented when connection to a remote computer was interupted.
		ui.message(_("Connection interrupted"))

	def connectAsMaster(self, address, key, insecure=False):
		transport = RelayTransport(address=address, serializer=serializer.JSONSerializer(), channel=key, connection_type='master', insecure=insecure)
		self.master_session = MasterSession(transport=transport, local_machine=self.localMachine)
		transport.callback_manager.register_callback(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED, self.on_certificate_as_master_failed)
		transport.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected_as_master)
		transport.callback_manager.register_callback(TransportEvents.CONNECTION_FAILED, self.on_connected_as_master_failed)
		transport.callback_manager.register_callback(TransportEvents.CLOSING, self.disconnecting_as_master)
		transport.callback_manager.register_callback(TransportEvents.DISCONNECTED, self.on_disconnected_as_master)
		self.master_transport = transport
		self.master_transport.reconnector_thread.start()

	def connect_AsSlave(self, address, key, insecure=False):
		transport = RelayTransport(serializer=serializer.JSONSerializer(), address=address, channel=key, connection_type='slave', insecure=insecure)
		self.slave_session = SlaveSession(transport=transport, local_machine=self.localMachine)
		self.slave_transport = transport
		transport.callback_manager.register_callback(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED, self.on_certificate_as_slave_failed)
		self.slave_transport.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected_as_slave)
		self.slave_transport.reconnector_thread.start()
		self.disconnect_item.Enable(True)
		self.connect_item.Enable(False)

	def handle_certificate_failed(self, transport):
		self.last_fail_address = transport.address
		self.last_fail_key = transport.channel
		self.disconnect()
		try:
			cert_hash = transport.last_fail_fingerprint

			wnd = dialogs.CertificateUnauthorizedDialog(None, fingerprint=cert_hash)
			a = wnd.ShowModal()
			if a == wx.ID_YES:
				config = configuration.get_config()
				config['trusted_certs'][hostport_to_address(self.last_fail_address)]=cert_hash
				config.write()
			if a == wx.ID_YES or a == wx.ID_NO: return True
		except Exception as ex:
			log.error(ex)
		return False

	def on_certificate_as_master_failed(self):
		if self.handle_certificate_failed(self.master_transport):
			self.connectAsMaster(self.last_fail_address, self.last_fail_key, True)

	def on_certificate_as_slave_failed(self):
		if self.handle_certificate_failed(self.slave_transport):
			self.connect_AsSlave(self.last_fail_address, self.last_fail_key, True)

	def on_connected_as_slave(self):
		log.info("Control connector connected")
		cues.control_server_connected()
		# Translators: Presented in direct (client to server) remote connection when the controlled computer is ready.
		speech.speakMessage(_("Connected to control server"))
		self.push_clipboard_item.Enable(True)
		self.copy_link_item.Enable(True)
		configuration.write_connection_to_config(self.slave_transport.address)

	def start_control_server(self, server_port, channel):
		self.server = server.Server(server_port, channel)
		server_thread = threading.Thread(target=self.server.run)
		server_thread.daemon = True
		server_thread.start()

	def hook(self):
		log.debug("Hook thread start")
		keyhook = keyboard_hook.KeyboardHook()
		keyhook.register_callback(self.hook_callback)
		msg = ctypes.wintypes.MSG()
		while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
			pass
		log.debug("Hook thread end")
		keyhook.free()

	def hook_callback(self, **kwargs):
		if not self.sending_keys:
			return False
		keyCode = (kwargs['vk_code'], kwargs['extended'])
		gesture = KeyboardInputGesture(self.keyModifiers, keyCode[0], kwargs['scan_code'], keyCode[1])
		if not kwargs['pressed'] and keyCode in self.hostPendingModifiers:
			self.hostPendingModifiers.discard(keyCode)
			return False
		gesture = KeyboardInputGesture(self.keyModifiers, keyCode[0], kwargs['scan_code'], keyCode[1])
		if gesture.isModifier:
			if kwargs['pressed']:
				self.keyModifiers.add(keyCode)
			else:
				self.keyModifiers.discard(keyCode)
		elif kwargs['pressed']:
			script = gesture.script
			if script in self.localScripts:
				wx.CallAfter(script, gesture)
				return True
		self.master_transport.send(type="key", **kwargs)
		return True #Don't pass it on



	@script(
		# Translators: Documentation string for the script that toggles the control between guest and host machine.
		description=_("Toggles the control between guest and host machine"),
		gesture="kb:f11",
		)
	def script_sendKeys(self, gesture):
		if not self.master_transport:
			gesture.send()
			return
		self.sending_keys = not self.sending_keys
		self.set_receiving_braille(self.sending_keys)
		if self.sending_keys:
			self.hostPendingModifiers = gesture.modifiers
			# Translators: Presented when sending keyboard keys from the controlling computer to the controlled computer.
			ui.message(_("Controlling remote machine."))
		else:
			self.releaseKeys()
			# Translators: Presented when keyboard control is back to the controlling computer.
			ui.message(_("Controlling local machine."))

	def releaseKeys(self):
		# release all pressed keys in the guest.
		for k in self.keyModifiers:
			self.master_transport.send(type="key", vk_code=k[0], extended=k[1], pressed=False)
		self.keyModifiers = set()

	def set_receiving_braille(self, state):
		if state and self.master_session.patch_callbacks_added and braille.handler.enabled:
			self.master_session.patcher.patch_braille_input()
			if versionInfo.version_year < 2023:
				braille.handler.enabled = False
				if braille.handler._cursorBlinkTimer:
					braille.handler._cursorBlinkTimer.Stop()
					braille.handler._cursorBlinkTimer=None
				if braille.handler.buffer is braille.handler.messageBuffer:
					braille.handler.buffer.clear()
					braille.handler.buffer = braille.handler.mainBuffer
					if braille.handler._messageCallLater:
						braille.handler._messageCallLater.Stop()
						braille.handler._messageCallLater = None
			self.localMachine.receiving_braille=True
		elif not state:
			self.master_session.patcher.unpatch_braille_input()
			if versionInfo.version_year < 2023:
				braille.handler.enabled = bool(braille.handler.displaySize)
			self.localMachine.receiving_braille=False

	if versionInfo.version_year < 2024:
		def event_gainFocus(self, obj, nextHandler):
			if isinstance(obj, IAccessibleHandler.SecureDesktopNVDAObject):
				self.sd_focused = True
				self.enter_secure_desktop()
			elif self.sd_focused and not isinstance(obj, IAccessibleHandler.SecureDesktopNVDAObject):
				#event_leaveFocus won't work for some reason
				self.sd_focused = False
				self.leave_secure_desktop()
			nextHandler()

	# For NVDA 2024.1 and above
	def onSecureDesktopChange(self, isSecureDesktop: bool):
		'''
		@param isSecureDesktop: True if the new desktop is the secure desktop.
		'''
		if isSecureDesktop:
			self.enter_secure_desktop()
		else:
			self.leave_secure_desktop()

	def enter_secure_desktop(self):
		"""function ran when entering a secure desktop."""
		if self.slave_transport is None:
			return
		if not os.path.exists(self.temp_location):
			os.makedirs(self.temp_location)
		channel = str(uuid.uuid4())
		self.sd_server = server.Server(port=0, password=channel, bind_host='127.0.0.1')
		port = self.sd_server.server_socket.getsockname()[1]
		server_thread = threading.Thread(target=self.sd_server.run)
		server_thread.daemon = True
		server_thread.start()
		self.sd_relay = RelayTransport(address=('127.0.0.1', port), serializer=serializer.JSONSerializer(), channel=channel, insecure=True)
		self.sd_relay.callback_manager.register_callback('msg_client_joined', self.on_master_display_change)
		self.slave_transport.callback_manager.register_callback('msg_set_braille_info', self.on_master_display_change)
		self.sd_bridge = bridge.BridgeTransport(self.slave_transport, self.sd_relay)
		relay_thread = threading.Thread(target=self.sd_relay.run)
		relay_thread.daemon = True
		relay_thread.start()
		data = [port, channel]
		with open(self.ipc_file, 'w') as fp:
			json.dump(data, fp)

	def leave_secure_desktop(self):
		if self.sd_server is None:
			return #Nothing to do
		self.sd_bridge.disconnect()
		self.sd_bridge = None
		self.sd_server.close()
		self.sd_server = None
		self.sd_relay.close()
		self.sd_relay = None
		self.slave_transport.callback_manager.unregister_callback('msg_set_braille_info', self.on_master_display_change)
		self.slave_session.set_display_size()

	def on_master_display_change(self, **kwargs):
		self.sd_relay.send(type='set_display_size', sizes=self.slave_session.master_display_sizes)

	SD_CONNECT_BLOCK_TIMEOUT = 1
	def handle_secure_desktop(self):
		try:
			with open(self.ipc_file) as fp:
				data = json.load(fp)
			os.unlink(self.ipc_file)
			port, channel = data
			test_socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			test_socket=ssl.wrap_socket(test_socket)
			test_socket.connect(('127.0.0.1', port))
			test_socket.close()
			self.connect_AsSlave(('127.0.0.1', port), channel, insecure=True)
			# So we don't miss the first output when switching to a secure desktop,
			# block the main thread until the connection is established. We're
			# connecting to localhost, so this should be pretty fast. Use a short
			# timeout, though.
			self.slave_transport.connected_event.wait(self.SD_CONNECT_BLOCK_TIMEOUT)
		except:
			pass

	def verify_connect(self, con_info):
		if self.is_connected() or self.connecting:
			gui.messageBox(_("NVDA Remote is already connected. Disconnect before opening a new connection."), _("NVDA Remote Already Connected"), wx.OK|wx.ICON_WARNING)
			return
		self.connecting = True
		server_addr = con_info.get_address()
		key = con_info.key
		if con_info.mode == 'master':
			message = _("Do you wish to control the machine on server {server} with key {key}?").format(server=server_addr, key=key)
		elif con_info.mode == 'slave':
			message = _("Do you wish to allow this machine to be controlled on server {server} with key {key}?").format(server=server_addr, key=key)
		if gui.messageBox(message, _("NVDA Remote Connection Request"), wx.YES|wx.NO|wx.NO_DEFAULT|wx.ICON_WARNING) != wx.YES:
			self.connecting = False
			return
		if con_info.mode == 'master':
			self.connectAsMaster((con_info.hostname, con_info.port), key=key)
		elif con_info.mode == 'slave':
			self.connect_AsSlave((con_info.hostname, con_info.port), key=key)
		self.connecting = False

	def is_connected(self):
		connector = self.slave_transport or self.master_transport
		if connector is not None:
			return connector.connected
		return False

	__gestures = {
		"kb:alt+NVDA+pageDown": "disconnect",
		"kb:alt+NVDA+pageUp": "connect",
		"kb:control+shift+NVDA+c": "push_clipboard",
	}
