# Jukebox for OpenATV
# SPDX-License-Identifier: GPL-3.0-only


from contextlib import contextmanager
from os import W_OK, access, listdir, makedirs, unlink, walk
from os.path import basename, dirname, exists, isdir, isfile, join
from queue import Empty, Queue
from random import shuffle
from re import fullmatch
from sqlite3 import Error as DatabaseError, connect
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from time import time
from twisted.internet.threads import deferToThread

from enigma import RT_HALIGN_CENTER, RT_HALIGN_LEFT, RT_HALIGN_RIGHT, RT_VALIGN_CENTER, eEPGCache, eListbox, eListboxPythonMultiContent, ePicLoad, ePoint, ePythonMessagePump, eServiceCenter, eServiceReference, eTimer, gFont, getBestPlayableServiceReference, getDesktop, iPlayableService, iServiceInformation

from ServiceReference import ServiceReference
from skin import fonts, getSkinFactor, parseFont
from Components.ActionMap import ActionMap, HelpableActionMap, NumberActionMap, loadKeymap
from Components.config import ConfigDirectory, ConfigInteger, ConfigSubsection, ConfigText, ConfigYesNo, config
from Components.FileList import FILE_IS_DIR, FILE_NAME, FileList
from Components.GUIComponent import GUIComponent
from Components.Label import Label
from Components.Pixmap import MultiPixmap, Pixmap
from Components.ScrollLabel import ScrollLabel
from Components.ServiceEventTracker import InfoBarBase, ServiceEventTracker
from Components.ServicePosition import ServicePositionGauge
from Components.SystemInfo import BoxInfo
from Components.VideoWindow import VideoWindow
from Components.Sources.StaticText import StaticText
from Plugins.Plugin import PluginDescriptor
from Screens.ChoiceBox import ChoiceBox
from Screens.EpgSelection import EPGSelection
from Screens.EventView import EventViewEPGSelect
from Screens.InfoBar import InfoBar
from Screens.InfoBarGenerics import InfoBarNotifications, InfoBarSeek, NumberZap
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from Screens.VirtualKeyBoard import VirtualKeyBoard
from Tools.BoundFunction import boundFunction
from Tools.Directories import SCOPE_GUISKIN, SCOPE_PLAYLIST, resolveFilename
from Tools.LoadPixmap import LoadPixmap

from . import PluginLanguageDomain, _
from .appearance import playerAppearance
from .covers import CoverRequest
from .display import JukeboxDisplayListSummary, JukeboxDisplaySummary
from .editing import MP3TagWriter
from .lyrics import LyricsRequest, TimedLyrics
from .metadata import AudioMetadata
from .playlistio import PlaylistFile
from .playlists import PlaylistReader


loadKeymap(join(dirname(__file__), "keymap.xml"))

config.plugins.jukebox = ConfigSubsection()
config.plugins.jukebox.resumePlaylist = ConfigYesNo(default=True)
config.plugins.jukebox.lastSongIndex = ConfigInteger(-1)
config.plugins.jukebox.databasePath = ConfigDirectory(default="/media/hdd/jukebox")
config.plugins.jukebox.screenSaver = ConfigYesNo(default=True)
config.plugins.jukebox.screenSaverDelay = ConfigInteger(1, limits=(1, 60))
config.plugins.jukebox.playerInExtensions = ConfigYesNo(default=True)
config.plugins.jukebox.musicFolder = ConfigDirectory(default="/media/hdd/")
config.plugins.jukebox.rememberFolder = ConfigYesNo(default=True)
config.plugins.jukebox.playerInMainMenu = ConfigYesNo(default=False)


config.plugins.jukebox.showCover = ConfigYesNo(default=True)
config.plugins.jukebox.onlineCovers = ConfigYesNo(default=True)
config.plugins.jukebox.onlineLyrics = ConfigYesNo(default=True)
config.plugins.jukebox.coverCache = ConfigDirectory(default="/media/hdd/jukebox/covers")


class MessageQueue:
	def __init__(self):
		self.queue = Queue()

	def push(self, value):
		self.queue.put(value)

	def pop(self):
		try:
			return self.queue.get_nowait()
		except Empty:
			return None


class LibraryScanner:
	WORKING = 1
	FINISHED = 2

	def __init__(self):
		self.isRunning = False
		self.cancelEvent = Event()
		self.messages = MessageQueue()
		self.messagePump = ePythonMessagePump()

	def cancel(self):
		self.cancelEvent.set()

	def start(self, path):
		if not self.isRunning:
			self.isRunning = True
			self.cancelEvent.clear()
			Thread(target=self.run, args=(path,), daemon=True).start()

	def sendMessage(self, state, text):
		self.messages.push((state, text))
		self.messagePump.send(0)

	def run(self, path):
		connection = None
		counter = 0
		try:
			connection = musicDatabase.open()
			if connection is None:
				raise OSError("Cannot open music library")
			checkTime = 0
			for root, folders, files in walk(path):
				if self.cancelEvent.is_set():
					break
				for fileName in sorted(files):
					if self.cancelEvent.is_set():
						break
					if not fileName.lower().endswith(AudioMetadata.EXTENSIONS):
						continue
					metadata = AudioMetadata(join(root, fileName))
					if metadata.audio is None:
						continue
					identifiers = []
					for table, column, idColumn, value in (
						("Artists", "artist", "artist_id", metadata.artist),
						("Album", "album_text", "album_id", metadata.album),
						("Genre", "genre_text", "genre_id", metadata.genre),
					):
						connection.execute(f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)", (value,))
						identifiers.append(connection.execute(f"SELECT {idColumn} FROM {table} WHERE {column} = ?", (value,)).fetchone()[0])
					values = (metadata.fileName, metadata.title, *identifiers, metadata.trackNumber, metadata.bitrate, metadata.length, metadata.track, metadata.date)
					connection.execute(
						"INSERT INTO Songs (filename,title,artist_id,album_id,genre_id,tracknumber,bitrate,length,track,date) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(filename) DO UPDATE SET title=excluded.title,artist_id=excluded.artist_id,album_id=excluded.album_id,genre_id=excluded.genre_id,tracknumber=excluded.tracknumber,bitrate=excluded.bitrate,length=excluded.length,track=excluded.track,date=excluded.date",
						values,
					)
					counter += 1
					if time() - checkTime >= 0.2:
						self.sendMessage(LibraryScanner.WORKING, _("%s\n added to database") % metadata.fileName)
						checkTime = time()
			if self.cancelEvent.is_set():
				connection.rollback()
				message = _("Process aborted.\n 0 files added to database!\nPress OK to close.")
			else:
				connection.commit()
				message = _("%d files added to database!\nPress OK to close.") % counter
		except (OSError, DatabaseError, ValueError, TypeError) as error:
			message = _("Database error: %s") % error
		finally:
			if connection is not None:
				connection.close()
			self.isRunning = False
		self.sendMessage(LibraryScanner.FINISHED, message)


libraryScanner = LibraryScanner()


class JukeboxScanner(Screen):
	def __init__(self, session, initialDirectory):
		Screen.__init__(self, session)
		self.setTitle(_("Add music files to music library"))
		self.skin = playerAppearance.load("JukeboxScanner")
		self["actions"] = ActionMap(
			["WizardActions", "ColorActions"],
			{
				"back": self.cancel,
				"green": self.green,
				"red": self.cancel,
				"ok": self.green,
			},
			-1,
		)
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Close"))
		self["output"] = Label()
		self.onClose.append(self.onCloseCallback)
		libraryScanner.messagePump.recv_msg.get().append(self.scanMessageReceived)
		if not libraryScanner.isRunning and initialDirectory:
			libraryScanner.start(initialDirectory)

	def scanMessageReceived(self, message):
		message = libraryScanner.messages.pop()
		if message is None:
			return
		self["output"].setText(message[1])
		if message[0] == LibraryScanner.FINISHED:
			self["key_red"].setText("")

	def green(self):
		self.close()

	def cancel(self):
		if libraryScanner.isRunning:
			libraryScanner.cancel()

	def onCloseCallback(self):
		libraryScanner.messagePump.recv_msg.get().remove(self.scanMessageReceived)


class LibraryAction:
	def __init__(self, callback=None, arguments=None):
		self.callback = callback
		self.arguments = arguments


class LibraryViewState:
	def __init__(self, cache=True, index=0, entries=None, headerText="", action=None):
		self.cache = cache
		self.index = index
		self.entries = [] if entries is None else entries
		self.headerText = headerText
		self.action = action


class MusicItem:
	def __init__(
		self,
		text="",
		mode=0,
		navigator=False,
		artistId=0,
		albumId=0,
		title="",
		artist="",
		fileName="",
		bitrate=None,
		length="",
		genre="",
		track="",
		date="",
		album="",
		playlistId=0,
		genreId=0,
		songId=0,
		formatMetadata=True,
		cuePts=None,
		serviceReference="",
	):
		self.text = text
		self.mode = mode
		self.navigator = navigator
		self.artistId = artistId
		self.albumId = albumId
		self.title = title
		self.artist = artist
		self.fileName = fileName
		if bitrate is not None:
			if formatMetadata:
				self.bitrate = f"{bitrate:d} Kbps"
			else:
				self.bitrate = bitrate
		else:
			self.bitrate = ""
		self.length = AudioMetadata.text(length)
		self.genre = genre
		if track and formatMetadata:
			self.track = _("Track %s") % track
		elif track:
			self.track = track
		else:
			self.track = ""
		if date is not None:
			if formatMetadata:
				self.date = f" ({date})"
			else:
				self.date = date
		else:
			self.date = ""

		self.album = album
		self.playlistId = playlistId
		self.genreId = genreId
		self.songId = songId
		self.cuePts = cuePts
		self.serviceReference = serviceReference


class MusicDatabase:
	def open(self):
		databasePath = join(config.plugins.jukebox.databasePath.value, "Jukebox.db")
		connection = None
		try:
			makedirs(dirname(databasePath), exist_ok=True)
			connection = connect(databasePath)
			if not access(databasePath, W_OK):
				raise OSError("Database file is not writable")
			connection.execute(
				"CREATE TABLE IF NOT EXISTS Songs (song_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL UNIQUE, title TEXT, artist_id INTEGER, album_id INTEGER, genre_id INTEGER, tracknumber INTEGER, bitrate INTEGER, length TEXT, track TEXT, date TEXT, lyrics TEXT);"
			)
			connection.execute("CREATE TABLE IF NOT EXISTS Artists (artist_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, artist TEXT NOT NULL UNIQUE);")
			connection.execute("CREATE TABLE IF NOT EXISTS Album (album_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, album_text TEXT NOT NULL UNIQUE);")
			connection.execute("CREATE TABLE IF NOT EXISTS Genre (genre_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, genre_text TEXT NOT NULL UNIQUE);")
			connection.execute("CREATE TABLE IF NOT EXISTS Playlists (playlist_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, playlist_text TEXT NOT NULL);")
			connection.execute("CREATE TABLE IF NOT EXISTS Playlist_Songs (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL, song_id INTEGER NOT NULL);")
			connection.execute(
				"CREATE TABLE IF NOT EXISTS CurrentSongList (ID INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, song_id INTEGER, filename TEXT NOT NULL, title TEXT, artist TEXT, album TEXT, genre TEXT, bitrate TEXT, length TEXT, track TEXT, date TEXT, PTS INTEGER);"
			)
		except (OSError, DatabaseError) as error:
			print(f"[Jukebox] Cannot open database {databasePath}: {error}")
			if connection is not None:
				connection.close()
			connection = None
		return connection

	@contextmanager
	def connection(self):
		connection = self.open()
		try:
			yield connection
		finally:
			if connection is not None:
				connection.close()

	def readSong(self, songId):
		with self.connection() as connection:
			if connection is None:
				raise OSError("Cannot open music database")
			row = connection.execute("SELECT Songs.title, Artists.artist, Album.album_text, Genre.genre_text, Songs.track, Songs.date FROM Songs LEFT JOIN Artists USING (artist_id) LEFT JOIN Album USING (album_id) LEFT JOIN Genre USING (genre_id) WHERE song_id = ?", (songId,)).fetchone()
			if row is None:
				raise ValueError("The song is no longer in the database")
			return dict(zip(("title", "artist", "album", "genre", "track", "date"), (x or "" for x in row)))

	def updateSong(self, songId, values):
		with self.connection() as connection:
			if connection is None:
				raise OSError("Cannot open music database")
			row = connection.execute("SELECT filename FROM Songs WHERE song_id = ?", (songId,)).fetchone()
			if row is None:
				raise ValueError("The song is no longer in the database")
			identifiers = []
			for table, column, idColumn, field in (("Artists", "artist", "artist_id", "artist"), ("Album", "album_text", "album_id", "album"), ("Genre", "genre_text", "genre_id", "genre")):
				connection.execute(f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)", (values[field],))
				identifiers.append(connection.execute(f"SELECT {idColumn} FROM {table} WHERE {column} = ?", (values[field],)).fetchone()[0])
			try:
				trackNumber = int(values["track"].split("/", 1)[0])
			except ValueError:
				trackNumber = -1
			connection.execute("UPDATE Songs SET title=?, artist_id=?, album_id=?, genre_id=?, track=?, tracknumber=?, date=? WHERE song_id=?", (values["title"], *identifiers, values["track"], trackNumber, values["date"], songId))
			connection.execute("UPDATE CurrentSongList SET title=?, artist=?, album=?, genre=?, track=?, date=? WHERE filename=?", (values["title"], values["artist"], values["album"], values["genre"], values["track"], values["date"], row[0]))
			connection.commit()


musicDatabase = MusicDatabase()


class JukeboxScreenSaver(Screen):
	MOVING_WIDGETS = ("cabinet", "coverArt", "brand", "nowPlaying", "display", "artist", "album")

	def __init__(self, session):
		self.session = session
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxScreenSaver")
		self["actions"] = ActionMap(
			["WizardActions", "DirectionActions", "ColorActions", "EventViewActions"],
			{
				"back": self.close,
				"right": self.close,
				"left": self.close,
				"up": self.close,
				"down": self.close,
				"ok": self.close,
				"pageUp": self.close,
				"pageDown": self.close,
				"yellow": self.close,
				"blue": self.close,
				"red": self.close,
				"green": self.close,
				"prevBouquet": self.close,
				"nextBouquet": self.close,
				"info": self.close,
			},
			-1,
		)
		self["coverArt"] = CoverPixmap()
		self.coverMoveTimer = eTimer()
		self.coverMoveTimer.timeout.get().append(self.moveCoverArt)
		self.saverOffsets = None
		self.onLayoutFinish.append(self.moveCoverArt)
		self.onClose.append(self.coverMoveTimer.stop)
		self["cabinet"] = Pixmap()
		self["brand"] = Label(_("Jukebox"))
		self["nowPlaying"] = Label(_("NOW PLAYING"))
		self["display"] = Label()
		self["artist"] = Label()
		self["album"] = Label()

	def updateDisplayText(self, text, artist="", album=""):
		self["display"].setText(text)
		self["artist"].setText(artist)
		self["album"].setText(album)

	def updateLCD(self, text, line):
		self.summaries.setText(text, line)

	def updateCover(self, fileName=None, coverMode=0):
		print(f"[JukeboxScreenSaver] updating coverart with filename = {fileName} and modus = {coverMode:d}")
		if coverMode == 0:
			if fileName:
				self["coverArt"].showCoverFromFile(fileName)
			else:
				self["coverArt"].showDefaultCover()
		elif coverMode == 1:
			self["coverArt"].showDefaultCover()
		elif coverMode == 2:
			self["coverArt"].embeddedCoverArt()
		elif coverMode == 3:
			self["coverArt"].updateCoverArt(fileName)
		elif coverMode == 4:
			self["coverArt"].showCoverFromFile(fileName)

	def moveCoverArt(self):
		if any(self[x].instance is None for x in self.MOVING_WIDGETS):
			return
		screenSize = self.instance.size()
		cardSize = self["cabinet"].instance.size()
		factor = cardSize.width() / 900
		margin = round(24 * factor)
		right = max(margin, screenSize.width() - cardSize.width() - margin)
		bottom = max(margin, screenSize.height() - cardSize.height() - margin)
		if self.saverOffsets is None:
			self.saverOffsets = {x: (self[x].instance.position().x(), self[x].instance.position().y()) for x in self.MOVING_WIDGETS}
			self.saverPosition = ((screenSize.width() - cardSize.width()) // 2, (screenSize.height() - cardSize.height()) // 2)
			step = max(1, round(2 * factor))
			self.saverDirection = (step, step)
		x, y = self.saverPosition
		deltaX, deltaY = self.saverDirection
		if not margin <= x + deltaX <= right:
			deltaX = -deltaX
		if not margin <= y + deltaY <= bottom:
			deltaY = -deltaY
		x = max(margin, min(right, x + deltaX))
		y = max(margin, min(bottom, y + deltaY))
		self.saverPosition = (x, y)
		self.saverDirection = (deltaX, deltaY)
		for name, (offsetX, offsetY) in self.saverOffsets.items():
			self[name].move(ePoint(x + offsetX, y + offsetY))
		self.coverMoveTimer.start(1000, True)

	def createSummary(self):
		return JukeboxSummary


class JukeboxTV(JukeboxScreenSaver):
	def __init__(self, session, currentService, serviceList):
		self.session = session
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxTV")

		self.setTitle(_("Jukebox TV"))
		self.onClose.append(self.onCloseCallback)
		self["actions"] = ActionMap(
			["OkCancelActions", "DirectionActions", "ChannelSelectBaseActions", "ChannelSelectEPGActions"],
			{
				"cancel": self.close,
				"ok": self.showHide,
				"right": self.nextService,
				"left": self.prevService,
				"nextBouquet": self.nextBouquet,
				"prevBouquet": self.prevBouquet,
				"showEPGList": self.openEventView,
			},
			-1,
		)
		self["actions2"] = NumberActionMap(
			["NumberActions"],
			{
				"1": self.keyNumberGlobal,
				"2": self.keyNumberGlobal,
				"3": self.keyNumberGlobal,
				"4": self.keyNumberGlobal,
				"5": self.keyNumberGlobal,
				"6": self.keyNumberGlobal,
				"7": self.keyNumberGlobal,
				"8": self.keyNumberGlobal,
				"9": self.keyNumberGlobal,
			},
			-1,
		)
		self.epgCache = eEPGCache.getInstance()
		self.currentService = currentService
		self.serviceList = serviceList
		self.currentPiP = ""
		self["coverArt"] = CoverPixmap()
		self["display"] = Label()
		self["video"] = VideoWindow(fb_width=getDesktop(0).size().width(), fb_height=getDesktop(0).size().height())
		self.pipServiceRelation = {}
		selectedService = None
		try:
			if self.currentService:
				current = ServiceReference(self.currentService)
				selectedService = current.ref
		except Exception:
			selectedService = None
		if selectedService is None:
			try:
				if self.serviceList:
					current = ServiceReference(self.serviceList.getCurrentSelection())
					service = current.ref
					self.playService(service)
			except Exception:
				pass
		else:
			self.playService(selectedService)

		self.showHideTimer = eTimer()
		self.showHideTimer.timeout.get().append(self.showHideTimerTimeout)
		self.hideDelay = config.usage.infobar_timeout.index
		if self.hideDelay:
			self.showHideTimer.start(self.hideDelay * 1000)
		self.displayShown = True

	def showHide(self):
		if self.displayShown:
			if self.showHideTimer.isActive():
				self.showHideTimer.stop()
			self["coverArt"].hide()
			self["display"].hide()
		else:
			self["coverArt"].show()
			self["display"].show()
			if self.hideDelay:
				self.showHideTimer.start(self.hideDelay * 1000)
		self.displayShown = not self.displayShown

	def showHideTimerTimeout(self):
		self.showHide()

	def updateDisplayText(self, text, artist="", album=""):
		text = " — ".join(x for x in (text, artist, album) if x)
		if self.showHideTimer.isActive():
			self.showHideTimer.stop()
		self["display"].setText(text)
		self.displayShown = False
		self.showHide()

	def keyNumberGlobal(self, number):
		if self.serviceList is not None:
			self.session.openWithCallback(self.numberEntered, NumberZap, number, self.searchNumber)

	def numberEntered(self, service=None, bouquet=None):
		if service:
			self.selectAndStartService(service, bouquet)

	def searchNumberHelper(self, serviceHandler, channelNumber, bouquet):
		serviceList = serviceHandler.list(bouquet)
		if serviceList:
			serviceIterator = serviceList.getNext()
			while serviceIterator.valid():
				if channelNumber == serviceIterator.getChannelNum():
					return serviceIterator
				serviceIterator = serviceList.getNext()
		return None

	def searchNumber(self, number, firstBouquetOnly=False):
		bouquet = self.serviceList.getRoot()
		service = None
		serviceHandler = eServiceCenter.getInstance()
		if not firstBouquetOnly:
			service = self.searchNumberHelper(serviceHandler, number, bouquet)
		if config.usage.multibouquet.value and not service:
			bouquet = self.serviceList.bouquet_root
			bouquetIterator = serviceHandler.list(bouquet)
			if bouquetIterator:
				bouquet = bouquetIterator.getNext()
				while bouquet.valid():
					if bouquet.flags & eServiceReference.isDirectory:
						service = self.searchNumberHelper(serviceHandler, number, bouquet)
						if service:
							playable = not (service.flags & (eServiceReference.isMarker | eServiceReference.isDirectory)) or (service.flags & eServiceReference.isNumberedMarker)
							if not playable:
								service = None
							break
						if config.usage.alternative_number_mode.value or firstBouquetOnly:
							break
					bouquet = bouquetIterator.getNext()
		return service, bouquet

	def selectAndStartService(self, service, bouquet):
		if service:
			if self.serviceList.getRoot() != bouquet:
				self.serviceList.clearPath()
				if self.serviceList.bouquet_root != bouquet:
					self.serviceList.enterPath(self.serviceList.bouquet_root)
				self.serviceList.enterPath(bouquet)
			self.serviceList.setCurrentSelection(service)
			current = ServiceReference(self.serviceList.getCurrentSelection())
			self.playService(current.ref)

	def nextService(self):
		if self.serviceList is not None:
			if self.serviceList.inBouquet():
				previousSelection = self.serviceList.getCurrentSelection()
				if previousSelection:
					previousSelection = previousSelection.toString()
					while True:
						if config.usage.quickzap_bouquet_change.value and self.serviceList.atEnd():
							self.serviceList.nextBouquet()
						else:
							self.serviceList.moveDown()
						currentSelection = self.serviceList.getCurrentSelection()
						if not currentSelection or (not (currentSelection.flags & 64)) or currentSelection.toString() == previousSelection:
							break
			else:
				self.serviceList.moveDown()
			if self.isPlayable():
				current = ServiceReference(self.serviceList.getCurrentSelection())
				self.playService(current.ref)
			else:
				self.nextService()

	def prevService(self):
		if self.serviceList is not None:
			if self.serviceList.inBouquet():
				previousSelection = self.serviceList.getCurrentSelection()
				if previousSelection:
					previousSelection = previousSelection.toString()
					while True:
						if config.usage.quickzap_bouquet_change.value:
							if self.serviceList.atBegin():
								self.serviceList.prevBouquet()
						self.serviceList.moveUp()
						currentSelection = self.serviceList.getCurrentSelection()
						if not currentSelection or (not (currentSelection.flags & 64)) or currentSelection.toString() == previousSelection:
							break
			else:
				self.serviceList.moveUp()
			if self.isPlayable():
				current = ServiceReference(self.serviceList.getCurrentSelection())
				self.playService(current.ref)
			else:
				self.prevService()

	def isPlayable(self):
		current = ServiceReference(self.serviceList.getCurrentSelection())
		return not (current.ref.flags & (eServiceReference.isMarker | eServiceReference.isDirectory))

	def nextBouquet(self):
		if self.serviceList is not None:
			if config.usage.multibouquet.value:
				self.serviceList.nextBouquet()
				current = ServiceReference(self.serviceList.getCurrentSelection())
				self.playService(current.ref)

	def prevBouquet(self):
		if self.serviceList is not None:
			if config.usage.multibouquet.value:
				self.serviceList.prevBouquet()
				current = ServiceReference(self.serviceList.getCurrentSelection())
				self.playService(current.ref)

	def openSingleServiceEPG(self):
		if self.serviceList is not None:
			current = ServiceReference(self.serviceList.getCurrentSelection())
			self.session.open(EPGSelection, current.ref)

	def openEventView(self):
		if self.serviceList is not None:
			events = []
			self.events = events
			service = ServiceReference(self.serviceList.getCurrentSelection())
			ref = service.ref
			event = self.epgCache.lookupEventTime(ref, -1)
			if event:
				events.append(event)
			event = self.epgCache.lookupEventTime(ref, -1, 1)
			if event:
				events.append(event)
			if events:
				self.session.open(EventViewEPGSelect, events[0], service, self.eventViewCallback, self.openSingleServiceEPG, self.openMultiServiceEPG, self.openSimilarList)

	def eventViewCallback(self, setEvent, setService, val):
		events = self.events
		if len(events) > 1:
			events[0], events[1] = events[1], events[0]
			setEvent(events[0])

	def getBouquetServices(self, bouquet):
		services = []
		serviceIterator = eServiceCenter.getInstance().list(bouquet)
		if serviceIterator is not None:
			while True:
				service = serviceIterator.getNext()
				if not service.valid():
					break
				if service.flags & (eServiceReference.isDirectory | eServiceReference.isMarker):
					continue
				services.append(ServiceReference(service))
		return services

	def openMultiServiceEPG(self):
		if self.serviceList is not None:
			bouquet = self.serviceList.getRoot()
			services = self.getBouquetServices(bouquet)
			if services:
				self.session.open(EPGSelection, services)

	def openSimilarList(self, eventId, referenceString):
		self.session.open(EPGSelection, referenceString, None, eventId)

	def playService(self, service):
		selectedService = service
		relatedService = self.pipServiceRelation.get(service.toString(), None)
		if relatedService is not None:
			service = eServiceReference(relatedService)
		if service and (service.flags & eServiceReference.isGroup):
			ref = getBestPlayableServiceReference(service, eServiceReference())
		else:
			ref = service
		if ref and ref.toString() != self.currentPiP:
			self.pipService = eServiceCenter.getInstance().play(ref)
			if self.pipService and not self.pipService.setTarget(1):
				self.pipService.start()
				if self.serviceList is not None:
					self.serviceList.setCurrentSelection(selectedService)
				self.currentPiP = selectedService.toString()
			else:
				self.pipService = None
				self.currentPiP = ""
		else:
			self.pipService = None
			self.currentPiP = ""

	def onCloseCallback(self):
		self.pipService = None
		self.currentPiP = ""
		if self.showHideTimer.isActive():
			self.showHideTimer.stop()


class JukeboxPlayer(Screen, InfoBarBase, InfoBarSeek, InfoBarNotifications):
	RETURN_DELAY = 7000

	def __init__(self, session, songList, index, libraryMode, currentService, serviceList, browser=None):
		self.session = session
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxPlayer")
		InfoBarNotifications.__init__(self)
		InfoBarBase.__init__(self)
		self["actions"] = ActionMap(
			["WizardActions", "JukeboxPlaybackActions", "MenuActions", "ColorActions"],
			{
				"back": self.closePlayer,
				"pause": self.pauseEntry,
				"play": self.unPauseService,
				"stop": self.stopEntry,
				"right": self.playNext,
				"left": self.playPrevious,
				"up": self.showPlaylist,
				"down": self.showPlaylist,
				"prevBouquet": self.shuffleList,
				"nextBouquet": self.repeatSong,
				"info": self.showLyrics,
				"red": self.closePlayer,
				"blue": self.showLyrics,
				"yellow": boundFunction(self.pauseEntry, False),
				"green": self.play,
				"menu": self.openSetup,
				"ok": self.showTV,
			},
			-1,
		)

		self.onClose.append(self.onCloseCallback)
		self.session.nav.stopService()
		self["PositionGauge"] = ServicePositionGauge(self.session.nav)
		self["coverArt"] = CoverPixmap()
		self["repeat"] = MultiPixmap()
		self["shuffle"] = MultiPixmap()
		self["dvrStatus"] = MultiPixmap()
		self["title"] = Label()
		self["album"] = Label()
		self["artist"] = Label()
		self["genre"] = Label()
		self["nextTitle"] = Label()
		self.eventTracker = ServiceEventTracker(
			screen=self,
			eventmap={
				iPlayableService.evUpdatedInfo: self.updatedInfo,
				iPlayableService.evUser + 10: self.audioDecodeError,
				iPlayableService.evUser + 12: self.pluginError,
				iPlayableService.evUpdateIDv3Cover: self.embeddedCoverArt,
				iPlayableService.evStart: self.serviceStarted,
			},
		)

		InfoBarSeek.__init__(self, actionmap="JukeboxSeekActions")
		self.songList = songList
		self.originalSongList = songList[:]
		self.currentIndex = max(0, min(index, len(songList) - 1))
		self.shuffle = False
		self.repeat = False
		self.currentFileName = ""
		self.metadata = None
		self.coverRequest = None
		self.coverGeneration = 0
		self.closed = False
		self.ptsTimer = eTimer()
		self.ptsTimer.callback.append(self.ptsTimerCallback)
		self["headertext"] = Label(_("Jukebox"))
		self["nowPlaying"] = StaticText(_("NOW PLAYING"))
		self["upNext"] = StaticText(_("UP NEXT"))
		self["details"] = Label()
		self["playbackState"] = Label(_("Play"))
		self["playbackMode"] = Label()
		for sourceName in ("AlbumArtist", "Composer", "TrackNumber", "DiscNumber", "Year", "Bitrate", "SampleRate", "Channels"):
			self[sourceName] = StaticText()
		self["coverStatus"] = Label()
		self["key_red"] = StaticText(_("Close"))
		self["key_green"] = StaticText(_("Play"))
		self["key_yellow"] = StaticText(_("Pause"))
		self["key_blue"] = StaticText(_("Tags / Lyrics"))

		self.playbackStarted = False
		self.onShown.append(self.onShownCallback)
		# for lcd
		self.currentTitle = ""
		self.nextTitle = ""
		self.screenSaverTimer = eTimer()
		self.screenSaverTimer.timeout.get().append(self.screenSaverTimerTimeout)
		self.screenSaverScreen = None

		self["playlistHint"] = StaticText(_("TEXT: Playlist editor"))
		self["playlistActions"] = ActionMap(["JukeboxPlaylistActions"], {"openPlaylists": self.showPlaylist}, -2)
		self.browser = browser
		self["libraryActions"] = ActionMap(["JukeboxLibraryActions"], {"openLibrary": self.openLibrary}, -2)
		self.libraryMode = libraryMode
		self.currentService = currentService
		self.serviceList = serviceList

	def embeddedCoverArt(self):
		if config.plugins.jukebox.showCover.value and (not self.metadata or not self.metadata.coverData):
			if isfile("/tmp/.id3coverart"):
				self["coverArt"].embeddedCoverArt()

	def screenSaverTimerTimeout(self):
		if config.plugins.jukebox.screenSaver.value:
			if self.screenSaverTimer.isActive():
				self.screenSaverTimer.stop()
			if not self.screenSaverScreen:
				self.screenSaverScreen = self.session.instantiateDialog(JukeboxScreenSaver)
				self.screenSaverScreen.player = self
				self.session.execDialog(self.screenSaverScreen)
				self.screenSaverScreen.updateLCD(self.currentTitle, 1)
				self.screenSaverScreen.updateLCD(self.nextTitle, 4)
				self.screenSaverScreen.updateDisplayText(self["title"].getText(), self["artist"].getText(), self["album"].getText())
				self.screenSaverScreen.updateCover(self["coverArt"].coverFileName, coverMode=0)

	def resetScreenSaverTimer(self, *args):
		if config.plugins.jukebox.screenSaver.value and config.plugins.jukebox.screenSaverDelay.value != 0:
			if self.screenSaverTimer.isActive():
				self.screenSaverTimer.stop()
			self.screenSaverTimer.start(config.plugins.jukebox.screenSaverDelay.value * 60000, True)

	def onShownCallback(self):
		if not self.playbackStarted:
			self.playbackStarted = True
			self["coverArt"].onShow()
			self.playSong(self.songList[self.currentIndex][0].fileName)
		else:
			self.summaries.setText(self.currentTitle, 1)
			self.summaries.setText(self.nextTitle, 4)
			if self.screenSaverScreen:
				self.screenSaverScreen.doClose()
				self.screenSaverScreen = None
		self.resetScreenSaverTimer()

	def onCloseCallback(self):
		self.stopSeeking()
		self.closed = True
		if self.screenSaverScreen:
			self.screenSaverScreen.doClose()
			self.screenSaverScreen = None
		self.coverGeneration += 1
		if self.coverRequest and not self.coverRequest.result.called:
			self.coverRequest.failed("Player closed")
		self.ptsTimer.stop()
		self.screenSaverTimer.stop()
		self.activityTimer.stop()
		self.seek = None

	def openLibrary(self):
		self.screenSaverTimer.stop()
		if isinstance(self.browser, JukeboxFileList):
			self.browser.libraryRequested = True
			self.browser.returnToPlayer = True
		self.close()

	def openSetup(self):
		if self.screenSaverTimer.isActive():
			self.screenSaverTimer.stop()
		self.session.openWithCallback(self.setupFinished, JukeboxSetup, False)

	def showTV(self):
		if BoxInfo.getItem("NumVideoDecoders", 1) > 1:
			if self.screenSaverTimer.isActive():
				self.screenSaverTimer.stop()
			if self.screenSaverScreen:
				self.screenSaverScreen.doClose()
				self.screenSaverScreen = None
			self.screenSaverScreen = self.session.instantiateDialog(JukeboxTV, self.currentService, self.serviceList)
			self.screenSaverScreen.player = self
			self.session.execDialog(self.screenSaverScreen)
			self.screenSaverScreen.updateLCD(self.currentTitle, 1)
			self.screenSaverScreen.updateLCD(self.nextTitle, 4)
			self.screenSaverScreen.updateDisplayText(self["title"].getText(), self["artist"].getText(), self["album"].getText())
			self.screenSaverScreen.updateCover(self["coverArt"].coverFileName, coverMode=0)

	def setupFinished(self, result=None):
		self.coverGeneration += 1
		if self.coverRequest and not self.coverRequest.result.called:
			self.coverRequest.failed("Settings changed")
		self.coverRequest = None
		self.updateCover("", "", "")
		if result == "library":
			self.openLibrary()
		else:
			self.resetScreenSaverTimer()

	def closePlayer(self):
		if config.plugins.jukebox.resumePlaylist.value:
			currentSong = self.songList[self.currentIndex][0] if self.songList else None
			config.plugins.jukebox.lastSongIndex.value = next((x for x, song in enumerate(self.originalSongList) if song[0] is currentSong), -1)
			config.plugins.jukebox.lastSongIndex.save()
			with musicDatabase.connection() as connection:
				if connection is not None:
					cursor = connection.cursor()
					cursor.execute("Delete from CurrentSongList;")
					for song in self.originalSongList:
						cursor.execute(
							"INSERT INTO CurrentSongList (song_id, filename,title,artist,album,genre, bitrate, length, track, date, PTS) VALUES(?,?,?,?,?,?,?,?,?,?,?);",
							(
								(
									song[0].songId,
									song[0].fileName,
									song[0].title,
									song[0].artist,
									song[0].album,
									song[0].genre,
									song[0].bitrate,
									song[0].length,
									song[0].track,
									song[0].date,
									song[0].cuePts,
								)
							),
						)
					connection.commit()
					cursor.close()
		if self.screenSaverTimer.isActive():
			self.screenSaverTimer.stop()
		self.close()

	def playSong(self, fileName):
		self.stopSeeking()
		self.ptsTimer.stop()
		self.coverGeneration += 1
		if self.coverRequest and not self.coverRequest.result.called:
			self.coverRequest.failed("Track changed")
		self.coverRequest = None
		self.session.nav.stopService()
		self.seek = None
		self.currentFileName = fileName
		self.metadata = AudioMetadata(fileName)
		song = self.songList[self.currentIndex][0]
		serviceReference = eServiceReference(song.serviceReference) if song.serviceReference else eServiceReference(4097, 0, fileName)
		if not self.metadata.audio and serviceReference.getName():
			self.metadata.title = serviceReference.getName()
		self.updateMusicInformation(self.metadata.artist, self.metadata.title, self.metadata.album, self.metadata.genre, self.metadata.date, clear=True)
		serviceReference.setName(self.metadata.title)
		self.session.nav.playService(serviceReference, adjust=False)
		if self.songList[self.currentIndex][0].cuePts is not None:
			self.updateCueInformation()
			self.cueSeekPending = True
			self.ptsTimer.start(250, True)
		self["nextTitle"].setText(self.getNextTitle())

	def ptsTimerCallback(self):
		if self.closed or not self.songList:
			return
		service = self.session.nav.getCurrentService()
		self.seek = service.seek() if service else None
		if self.seek:
			if self.cueSeekPending:
				self.seek.seekTo(self.songList[self.currentIndex][0].cuePts)
				self.cueSeekPending = False
			position = self.seek.getPlayPosition()
			if not position[0]:
				currentIndex = self.currentIndex
				for index, song in enumerate(self.songList):
					if song[0].fileName == self.currentFileName and song[0].cuePts is not None and position[1] >= song[0].cuePts:
						currentIndex = index
				if currentIndex != self.currentIndex:
					self.currentIndex = currentIndex
					self.updateCueInformation()
		self.ptsTimer.start(500, True)

	def updateCueInformation(self):
		self.updateSingleMusicInformation("artist", self.songList[self.currentIndex][0].artist, True)
		self.updateSingleMusicInformation("title", self.songList[self.currentIndex][0].title, True)
		self.updateSingleMusicInformation("album", self.songList[self.currentIndex][0].album, True)
		self.summaries.setText(self.songList[self.currentIndex][0].title, 1)
		if self.screenSaverScreen:
			self.screenSaverScreen.updateLCD(self.songList[self.currentIndex][0].title, 1)
			song = self.songList[self.currentIndex][0]
			self.screenSaverScreen.updateDisplayText(song.title, song.artist, song.album)
		self.updateCover(self.songList[self.currentIndex][0].artist, self.songList[self.currentIndex][0].album, self.songList[self.currentIndex][0].title)
		self.currentTitle = self.songList[self.currentIndex][0].title
		self["nextTitle"].setText(self.getNextTitle())

	def serviceStarted(self):
		self["dvrStatus"].setPixmapNum(0)

	def updatedInfo(self):
		if self.closed or not self.songList:
			return
		if self.songList[self.currentIndex][0].cuePts is not None:
			self.updateCueInformation()
		elif self.metadata and isfile(self.currentFileName):
			metadata = self.metadata
			self.updateMusicInformation(metadata.artist, metadata.title, metadata.album, metadata.genre, metadata.date, clear=True)
		else:
			service = self.session.nav.getCurrentService()
			info = service.info() if service else None
			if info:
				self.updateMusicInformation(
					info.getInfoString(iServiceInformation.sTagArtist),
					info.getInfoString(iServiceInformation.sTagTitle) or self.currentFileName,
					info.getInfoString(iServiceInformation.sTagAlbum),
					info.getInfoString(iServiceInformation.sTagGenre),
					clear=True,
				)

	def updateMusicInformation(self, artist="", title="", album="", genre="", year="", clear=False):
		if year and album:
			album = f"{album} {year}"
		else:
			album = album
		self.updateSingleMusicInformation("artist", artist, clear)
		self.updateSingleMusicInformation("title", title, clear)
		self.updateSingleMusicInformation("album", album, clear)
		self.updateSingleMusicInformation("genre", genre, clear)
		self.currentTitle = title
		if not self.libraryMode and self.songList[self.currentIndex][0].cuePts is None:
			# for lyrics
			self.songList[self.currentIndex][0].title = title
			self.songList[self.currentIndex][0].artist = artist
		self.summaries.setText(title, 1)
		if self.screenSaverScreen:
			self.screenSaverScreen.updateLCD(title, 1)
			self.screenSaverScreen.updateDisplayText(title, artist, album)
		self.updateCover(artist, album, title)

	def updateCover(self, artist, album, title):
		if not self.metadata or self.closed:
			return
		metadata = self.metadata
		details = [
			metadata.albumArtist,
			metadata.composer,
			(_("Track %s") % metadata.track if metadata.track else ""),
			(_("Disc %s") % metadata.disc if metadata.disc else ""),
			metadata.date,
		]
		for sourceName, value in (
			("AlbumArtist", metadata.albumArtist),
			("Composer", metadata.composer),
			("TrackNumber", metadata.track),
			("DiscNumber", metadata.disc),
			("Year", metadata.date),
			("Bitrate", metadata.bitrate),
			("SampleRate", metadata.sampleRate),
			("Channels", metadata.channels),
		):
			self[sourceName].setText(str(value))
		self["details"].setText(" · ".join(x for x in details if x))
		if not config.plugins.jukebox.showCover.value:
			self["coverArt"].hide()
			self["coverStatus"].setText("")
			return
		self["coverArt"].show()
		if metadata.coverData:
			self["coverArt"].showEmbeddedCover(metadata.coverData)
			self["coverStatus"].setText(_("Embedded cover"))
		elif metadata.coverFile():
			self["coverArt"].showCoverFromFile(metadata.coverFile())
			self["coverStatus"].setText(_("Cover from music folder"))
		elif config.plugins.jukebox.onlineCovers.value and (metadata.albumArtist or metadata.artist) and metadata.album:
			if self.coverRequest and self.coverRequest.artist == (metadata.albumArtist or metadata.artist) and self.coverRequest.album == metadata.album:
				if isfile(self.coverRequest.fileName):
					self.coverReady(self.coverGeneration, self.coverRequest.fileName)
				return
			self["coverArt"].showDefaultCover()
			self["coverStatus"].setText(_("Searching for cover…"))
			self.coverRequest = CoverRequest(metadata.albumArtist or metadata.artist, metadata.album, config.plugins.jukebox.coverCache.value)
			self.coverRequest.start().addCallback(boundFunction(self.coverReady, self.coverGeneration))
		else:
			self["coverArt"].showDefaultCover()
			self["coverStatus"].setText(_("No cover available"))

	def coverReady(self, generation, fileName):
		if self.closed or generation != self.coverGeneration:
			return
		if fileName:
			self["coverArt"].showCoverFromFile(fileName)
			self["coverStatus"].setText(_("Cover Art Archive / MusicBrainz"))
			if self.screenSaverScreen:
				self.screenSaverScreen.updateCover(fileName)
		else:
			self["coverStatus"].setText(_("No online cover found"))

	def updateSingleMusicInformation(self, name, info, clear):
		if info != "" or clear:
			if self[name].getText() != info:
				self[name].setText(info)

	def audioDecodeError(self):
		currentService = self.session.nav.getCurrentService()
		audioCodec = currentService.info().getInfoString(iServiceInformation.sUser + 10)
		print(f"[Jukebox] audio-codec {audioCodec} can't be decoded by hardware")
		self.session.open(MessageBox, _("This Receiver can't decode %s streams!") % audioCodec, type=MessageBox.TYPE_INFO, timeout=20)

	def pluginError(self):
		currentService = self.session.nav.getCurrentService()
		message = currentService.info().getInfoString(iServiceInformation.sUser + 12)
		print("[Jukebox]", message)
		self.session.open(MessageBox, message, type=MessageBox.TYPE_INFO, timeout=20)

	def doEofInternal(self, playing):
		if playing:
			self.playNext()

	def checkSkipShowHideLock(self):
		self.updatedSeekState()

	def updatedSeekState(self):
		paused = self.seekstate == self.SEEK_STATE_PAUSE
		self["dvrStatus"].setPixmapNum(1 if paused else 0)
		self["playbackState"].setText(_("Pause") if paused else _("Play"))
		self["playbackMode"].setText(" · ".join(x for x in ((_("Shuffle") if self.shuffle else ""), (_("Repeat") if self.repeat else "")) if x))

	def pauseEntry(self, toggle=True):
		self.stopSeeking()
		if toggle and self.seekstate == self.SEEK_STATE_PAUSE:
			self.unPauseService()
		elif self.seekstate != self.SEEK_STATE_PAUSE:
			self.pauseService()
		self.resetScreenSaverTimer()

	def play(self):
		if self.songList:
			self.unPauseService()
			self.resetScreenSaverTimer()

	def stopSeeking(self):
		if self.seekAction or self.LastseekAction:
			self.seekAction = 0
			self.LastseekAction = False
			self.doPause(False)

	def unPauseService(self):
		self.stopSeeking()
		if self.seekstate != self.SEEK_STATE_PLAY:
			self.setSeekState(self.SEEK_STATE_PLAY)

	def stopEntry(self):
		self.ptsTimer.stop()
		self.coverGeneration += 1
		if self.coverRequest and not self.coverRequest.result.called:
			self.coverRequest.failed("Playback stopped")
		self.seek = None
		self.session.nav.stopService()
		self.originalSongList = []
		self.songList = []
		if config.plugins.jukebox.resumePlaylist.value:
			config.plugins.jukebox.lastSongIndex.value = -1
			config.plugins.jukebox.lastSongIndex.save()
			with musicDatabase.connection() as connection:
				if connection is not None:
					cursor = connection.cursor()
					cursor.execute("Delete from CurrentSongList;")
					connection.commit()
					cursor.close()
		self.screenSaverTimer.stop()
		self.close()

	def playNext(self):
		if not self.songList:
			return
		if not self.repeat:
			if self.currentIndex + 1 > len(self.songList) - 1:
				self.currentIndex = 0
			else:
				self.currentIndex += 1
		if self.songList[self.currentIndex][0].cuePts is None:
			self.playSong(self.songList[self.currentIndex][0].fileName)
		else:
			self.playCueTrack()
		if not self.screenSaverScreen:
			self.resetScreenSaverTimer()

	def playPrevious(self):
		if not self.songList:
			return
		if not self.repeat:
			if self.currentIndex - 1 < 0:
				self.currentIndex = len(self.songList) - 1
			else:
				self.currentIndex -= 1

		if self.songList[self.currentIndex][0].cuePts is None:
			self.playSong(self.songList[self.currentIndex][0].fileName)
		else:
			self.playCueTrack()
		self.resetScreenSaverTimer()

	def getNextTitle(self):
		if not self.songList:
			return ""
		index = self.currentIndex if self.repeat else (self.currentIndex + 1) % len(self.songList)
		song = self.songList[index][0]
		if song.title:
			text = f"{song.title} — {song.artist}" if song.artist else song.title
		else:
			metadata = AudioMetadata(song.fileName)
			text = f"{metadata.title} — {metadata.artist}" if metadata.artist else metadata.title
		self.nextTitle = text
		self.summaries.setText(text, 4)
		if self.screenSaverScreen:
			self.screenSaverScreen.updateLCD(text, 4)
		return text

	def shuffleList(self):
		if not self.songList:
			return
		if self.songList[self.currentIndex][0].cuePts is None:  # not implemented for cue files yet
			self.shuffle = not self.shuffle
			if self.shuffle:
				self["shuffle"].setPixmapNum(1)
				shuffle(self.songList)
			else:
				self.songList = self.originalSongList[:]
				self["shuffle"].setPixmapNum(0)
			index = 0
			for x in self.songList:
				if x[0].fileName == self.currentFileName:
					self.currentIndex = index
					break
				index += 1
			self["nextTitle"].setText(self.getNextTitle())
			self.updatedSeekState()
		else:
			self.session.open(MessageBox, _("Shuffle is not available yet with cue-files!"), type=MessageBox.TYPE_INFO, timeout=20)
		self.resetScreenSaverTimer()

	def repeatSong(self):
		if not self.songList:
			return
		if self.songList[self.currentIndex][0].cuePts is None:  # not implemented for cue files yet
			self.repeat = not self.repeat
			if self.repeat:
				self["repeat"].setPixmapNum(1)
			else:
				self["repeat"].setPixmapNum(0)
			self["nextTitle"].setText(self.getNextTitle())
			self.updatedSeekState()
		else:
			self.session.open(MessageBox, _("Repeat is not available yet with cue-files!"), type=MessageBox.TYPE_INFO, timeout=20)
		self.resetScreenSaverTimer()

	def showPlaylist(self):
		if self.closed or not self.songList:
			return
		if self.screenSaverTimer.isActive():
			self.screenSaverTimer.stop()
		self.session.openWithCallback(self.showPlaylistCallback, JukeboxSongList, self.songList, self.currentIndex, self.libraryMode)

	def showPlaylistCallback(self, index=None):
		if self.closed:
			return
		if isinstance(index, tuple) and len(index) == 2:
			songList, index = index
			if not songList or not isinstance(index, int) or not 0 <= index < len(songList):
				self.resetScreenSaverTimer()
				return
			self.songList = songList[:]
			self.originalSongList = songList[:]
			self.shuffle = False
			self["shuffle"].setPixmapNum(0)
			self.libraryMode = False
		if isinstance(index, int) and 0 <= index < len(self.songList):
			self.currentIndex = index

			if self.songList[self.currentIndex][0].cuePts is None:
				self.playSong(self.songList[self.currentIndex][0].fileName)
			else:
				self.playCueTrack()

		self.resetScreenSaverTimer()

	def playCueTrack(self):
		song = self.songList[self.currentIndex][0]
		if song.fileName != self.currentFileName:
			self.playSong(song.fileName)
		else:
			self.cueSeekPending = True
			self.ptsTimer.start(1, True)
			self.updateCueInformation()

	def showLyrics(self):
		if self.closed or not self.songList:
			return
		if self.screenSaverTimer.isActive():
			self.screenSaverTimer.stop()
		self.session.openWithCallback(self.resetScreenSaverTimer, JukeboxTags, self.songList[self.currentIndex][0], self)

	def createSummary(self):
		return JukeboxSummary


class JukeboxTags(Screen):
	def __init__(self, session, currentSong, player=None):
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxTags")
		self.currentSong = currentSong
		self.player = player
		self.timedLyrics = TimedLyrics()
		self.displayedIndex = None
		self.syncTimer = eTimer()
		self.syncTimer.callback.append(self.updateLyrics)
		self.onShown.append(self.startSync)
		self.onHide.append(self.syncTimer.stop)
		self.lyricsRequest = None
		self.lyricsVisible = False
		self.closed = False
		self.onClose.append(self.cancelLyrics)
		self.metadata = AudioMetadata(currentSong.fileName)
		self["headertext"] = Label(self.metadata.title)
		self["resulttext"] = Label(self.metadata.artist)
		self["lyric_text"] = ScrollLabel()
		for name in ("previousLine", "currentLine", "nextLine"):
			self[name] = Label()
		self["key_blue"] = StaticText(_("Pause") if player else "")
		self["key_red"] = StaticText(_("Close"))
		self["key_green"] = StaticText(_("Lyrics"))
		self["key_yellow"] = StaticText(_("All tags"))
		self["actions"] = ActionMap(
			["OkCancelActions", "DirectionActions", "ColorActions", "JukeboxPlaybackActions"],
			{
				"cancel": self.close,
				"ok": self.close,
				"red": self.close,
				"green": self.startRun,
				"yellow": self.showTags,
				"blue": self.togglePause,
				"pause": self.togglePause,
				"up": self.pageUp,
				"left": self.pageUp,
				"down": self.pageDown,
				"right": self.pageDown,
			},
			-1,
		)
		self.onLayoutFinish.append(self.startRun)

	def createSummary(self):
		return JukeboxSummary

	def startRun(self):
		self.lyricsVisible = True
		self.setTitle(_("Lyrics"))
		self["resulttext"].setText(self.metadata.artist)
		self.timedLyrics = TimedLyrics.fromMetadata(self.metadata) if self.player else TimedLyrics()
		self.displayedIndex = None
		self.setSyncVisible()
		if self.timedLyrics.entries:
			self["resulttext"].setText(_("Synchronized lyrics"))
			self.updateLyrics()
			return
		text = self.metadata.lyricsText()
		if text or not config.plugins.jukebox.onlineLyrics.value:
			self["lyric_text"].setText(text or _("No lyrics found"))
			if self.player and text:
				self["resulttext"].setText(_("Lyrics without timestamps"))
			return
		self["lyric_text"].setText(_("Searching for lyrics…"))
		if self.lyricsRequest and self.lyricsRequest.result.called and self.lyricsRequest.error:
			self.lyricsRequest = None
		if self.lyricsRequest:
			if self.lyricsRequest.result.called:
				self.lyricsRequest.result.addCallback(self.lyricsFinished)
			return
		self.lyricsRequest = LyricsRequest(self.metadata, join(config.plugins.jukebox.databasePath.value, "lyrics"))
		self.lyricsRequest.start().addCallback(self.lyricsFinished)

	def lyricsFinished(self, result):
		if not self.closed and self.lyricsVisible:
			text = result.get("plainLyrics") or result.get("syncedLyrics")
			self.timedLyrics = TimedLyrics.fromText(result.get("syncedLyrics") or "") if self.player else TimedLyrics()
			self.displayedIndex = None
			self.setSyncVisible()
			if result.get("instrumental"):
				text = _("Instrumental — no lyrics")
			error = self.lyricsRequest.error
			missing = _("Online lyrics unavailable. Press green to retry.") if error and "404" not in error else _("No matching lyrics available")
			self["lyric_text"].setText(text or missing)
			if text:
				source = _("LRCLIB (cache)") if self.lyricsRequest.cached else _("LRCLIB")
				mode = _("Synchronized lyrics") if self.timedLyrics.entries else _("Lyrics without timestamps")
				self["resulttext"].setText(f"{source} · {mode}" if self.player else f"{self.metadata.artist} · {source}")
			self.updateLyrics()
		return result

	def cancelLyrics(self):
		self.closed = True
		self.syncTimer.stop()
		if self.lyricsRequest and not self.lyricsRequest.result.called:
			self.lyricsRequest.result.cancel()

	def showTags(self):
		self.lyricsVisible = False
		self.setSyncVisible()
		self["resulttext"].setText(self.metadata.artist)
		self.setTitle(_("All tags"))
		technical = f"{self.metadata.length} · {self.metadata.bitrate} kbit/s · {self.metadata.sampleRate} Hz · {self.metadata.channels} ch"
		self["lyric_text"].setText(f"{technical}\n\n{self.metadata.allTags()}")

	def pageUp(self):
		self["lyric_text"].pageUp()

	def pageDown(self):
		self["lyric_text"].pageDown()

	def setSyncVisible(self):
		synchronized = self.lyricsVisible and bool(self.timedLyrics.entries)
		for name in ("previousLine", "currentLine", "nextLine"):
			self[name].show() if synchronized else self[name].hide()
		self["lyric_text"].hide() if synchronized else self["lyric_text"].show()

	def startSync(self):
		if self.player and not self.closed:
			self.syncTimer.start(250)

	def togglePause(self):
		if self.player:
			self.player.pauseEntry()
			self.player.screenSaverTimer.stop()
			self.updateLyrics()

	def updateLyrics(self):
		if self.closed or not self.player:
			return
		player = self.player
		if player.closed or not player.songList:
			self.close()
			return
		player.screenSaverTimer.stop()
		song = player.songList[player.currentIndex][0]
		if song is not self.currentSong:
			self.currentSong = song
			# Detach before cancellation: its callback must not render the previous track.
			request, self.lyricsRequest = self.lyricsRequest, None
			visible, self.lyricsVisible = self.lyricsVisible, False
			if request and not request.result.called:
				request.result.cancel()
			self.metadata = AudioMetadata(song.fileName)
			self["headertext"].setText(self.metadata.title)
			self.startRun() if visible else self.showTags()
		self["key_blue"].setText(_("Play") if player.seekstate == player.SEEK_STATE_PAUSE else _("Pause"))
		if not self.lyricsVisible or not self.timedLyrics.entries:
			return
		service = self.session.nav.getCurrentService()
		seek = service.seek() if service else None
		position = seek.getPlayPosition() if seek else (1, 0)
		if position[0]:
			return
		index = self.timedLyrics.indexAt(position[1] // 90)
		if index != self.displayedIndex:
			self.displayedIndex = index
			self["previousLine"].setText(self.timedLyrics.lineAt(index - 1))
			self["currentLine"].setText(self.timedLyrics.lineAt(index) if index >= 0 else "♪")
			self["nextLine"].setText(self.timedLyrics.lineAt(index + 1))


class JukeboxSongList(Screen):
	def __init__(self, session, songList, index, libraryMode):
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxSongList")
		self.setTitle(_("Playlist editor"))
		self["headertext"] = Label(_("Playlist editor"))
		self["playlistHint"] = StaticText(_("MENU: Edit · CH+/CH−: Move"))
		self["list"] = LibraryList()
		self["list"].connectSelChanged(self.lcdUpdate)
		for key, label in (("red", _("Cancel")), ("green", _("Play")), ("yellow", _("Load")), ("blue", _("Save"))):
			self[f"key_{key}"] = StaticText(label)
		self["actions"] = ActionMap(["WizardActions", "ColorActions", "MenuActions"], {
			"ok": self.ok, "back": self.closing, "red": self.closing,
			"green": self.ok, "yellow": self.loadPlaylist, "blue": self.savePlaylist, "menu": self.editMenu,
		}, -1)
		self["editActions"] = ActionMap(["JukeboxPlaylistEditActions"], {
			"moveUp": boundFunction(self.moveEntry, -1), "moveDown": boundFunction(self.moveEntry, 1), "removeEntry": self.removeEntry,
		}, -2)
		self.songList = songList[:]
		self.index = index
		self.libraryMode = libraryMode
		self.changed = False
		self.dirty = False
		self.onLayoutFinish.append(self.startRun)

	def startRun(self):
		self["list"].setList(self.songList)
		self["list"].moveToIndex(max(0, min(self.index, len(self.songList) - 1)))
		self.lcdUpdate()

	def ok(self):
		if self.songList:
			index = self["list"].getCurrentIndex()
			self.close((self.songList, index) if self.changed else index)

	def closing(self):
		if self.dirty:
			self.session.openWithCallback(self.discardConfirmed, MessageBox, _("Discard unsaved playlist changes?"), default=False)
		else:
			self.close(-1)

	def discardConfirmed(self, answer):
		if answer:
			self.close(-1)

	def canEdit(self):
		if any(x[0].cuePts is not None for x in self.songList):
			self.session.open(MessageBox, _("CUE sheets can be played, but their track positions cannot be reordered or merged. Load a regular playlist to edit it."), type=MessageBox.TYPE_INFO)
			return False
		return True

	def editMenu(self):
		options = [(_("Add music files or playlist"), self.addFiles)]
		if self.songList:
			options.extend(((_("Remove selected entry"), self.removeEntry), (_("Move up"), boundFunction(self.moveEntry, -1)), (_("Move down"), boundFunction(self.moveEntry, 1)), (_("Clear playlist"), self.clearPlaylist)))
		self.session.openWithCallback(self.menuSelected, ChoiceBox, title=_("Edit playlist"), list=options)

	def menuSelected(self, selection):
		if selection:
			selection[1]()

	def refreshEntries(self, index):
		self.changed = self.dirty = True
		self["list"].setList(self.songList)
		self["list"].moveToIndex(max(0, min(index, len(self.songList) - 1)))
		self.lcdUpdate()

	def moveEntry(self, direction):
		if not self.canEdit():
			return
		index = self["list"].getCurrentIndex()
		target = index + direction
		if 0 <= index < len(self.songList) and 0 <= target < len(self.songList):
			self.songList[index], self.songList[target] = self.songList[target], self.songList[index]
			self.refreshEntries(target)

	def removeEntry(self):
		if not self.canEdit():
			return
		index = self["list"].getCurrentIndex()
		if 0 <= index < len(self.songList):
			del self.songList[index]
			self.refreshEntries(index)

	def clearPlaylist(self):
		self.session.openWithCallback(self.clearConfirmed, MessageBox, _("Clear the playlist? Music files will be kept."), default=False)

	def clearConfirmed(self, answer):
		if answer:
			self.songList = []
			self.refreshEntries(0)

	def addFiles(self):
		if not self.canEdit():
			return
		self.session.openWithCallback(boundFunction(self.fileSelected, True), JukeboxPlaylistFiles, "add")

	def loadPlaylist(self):
		if self.dirty:
			self.session.openWithCallback(self.loadConfirmed, MessageBox, _("Replace unsaved playlist changes?"), default=False)
		else:
			self.loadConfirmed(True)

	def loadConfirmed(self, answer):
		if answer:
			self.session.openWithCallback(boundFunction(self.fileSelected, False), JukeboxPlaylistFiles, "load")

	def fileSelected(self, append, fileName):
		if not fileName:
			return
		try:
			if isdir(fileName):
				entries = [{"fileName": join(fileName, x), "text": x} for x in sorted(listdir(fileName)) if x.lower().endswith(AudioMetadata.EXTENSIONS) and isfile(join(fileName, x))]
			elif fileName.lower().endswith(PlaylistFile.EXTENSIONS):
				entries = PlaylistFile.read(fileName)
			else:
				entries = [{"fileName": fileName, "text": basename(fileName)}]
			if not entries:
				raise ValueError(_("No playable entries found"))
			songs = [(MusicItem(**x),) for x in entries]
			if append and any(x[0].cuePts is not None for x in self.songList + songs):
				raise ValueError(_("CUE sheets cannot be merged with other playlists"))
			index = len(self.songList) if append else 0
			self.songList = self.songList + songs if append else songs
			self.refreshEntries(index)
			if not append:
				self.dirty = False
			self["headertext"].setText(_("Playlist: %s") % basename(fileName))
		except (OSError, ValueError) as error:
			self.session.open(MessageBox, _("Cannot read playlist: %s") % error, type=MessageBox.TYPE_ERROR)

	def savePlaylist(self):
		if not self.songList:
			self.session.open(MessageBox, _("The playlist is empty"), type=MessageBox.TYPE_INFO)
		elif any(x[0].cuePts is not None for x in self.songList):
			self.session.open(MessageBox, _("CUE track positions cannot be saved in MediaPlayer playlists"), type=MessageBox.TYPE_INFO)
		else:
			self.session.openWithCallback(self.saveSelected, JukeboxPlaylistFiles, "save")

	def saveSelected(self, fileName):
		if fileName:
			if exists(fileName):
				self.session.openWithCallback(boundFunction(self.overwriteConfirmed, fileName), MessageBox, _("Overwrite playlist %s?") % basename(fileName), default=False)
			else:
				self.writePlaylist(fileName)

	def overwriteConfirmed(self, fileName, answer):
		if answer:
			self.writePlaylist(fileName, True)

	def writePlaylist(self, fileName, overwrite=False):
		try:
			PlaylistFile.write(fileName, [x[0] for x in self.songList], overwrite)
			self.dirty = False
			self["headertext"].setText(_("Saved playlist: %s") % fileName)
		except (OSError, ValueError) as error:
			self.session.open(MessageBox, _("Cannot save playlist: %s") % error, type=MessageBox.TYPE_ERROR)

	def lcdUpdate(self):
		if self.songList:
			index = self["list"].getCurrentIndex()
			for offset, line in ((0, 1), (-1, 3), (1, 4)):
				song = self.songList[(index + offset) % len(self.songList)][0]
				self.summaries.setText(song.title or song.text, line)
		else:
			for line in (1, 3, 4):
				self.summaries.setText("", line)

	def createSummary(self):
		return JukeboxSummaryText


class JukeboxPlaylistFiles(Screen):
	def __init__(self, session, mode):
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxPlaylistFiles")
		self.mode = mode
		self.setTitle(_("Save playlist") if mode == "save" else _("Load playlist"))
		self["target"] = Label()
		extensions = "|".join(x[1:] for x in (PlaylistFile.WRITE_EXTENSIONS if mode == "save" else PlaylistFile.EXTENSIONS))
		if mode == "add":
			extensions = f"{extensions}|{'|'.join(x[1:] for x in AudioMetadata.EXTENSIONS)}"
		directory = config.plugins.jukebox.musicFolder.value if mode == "add" else resolveFilename(SCOPE_PLAYLIST)
		self["filelist"] = MusicFileList(directory if isdir(directory) else "/media", showDirectories=True, showFiles=True, matchingPattern=rf"(?i)^.*\.({extensions})$", useServiceRef=False)
		for key, label in (("red", _("Cancel")), ("green", _("Save here") if mode == "save" else (_("Add folder") if mode == "add" else _("Load"))), ("yellow", _("MediaPlayer")), ("blue", _("Music folder"))):
			self[f"key_{key}"] = StaticText(label)
		self["actions"] = ActionMap(["WizardActions", "DirectionActions", "ColorActions"], {
			"back": boundFunction(self.close, None), "red": boundFunction(self.close, None), "ok": self.selectFile,
			"green": self.green, "yellow": boundFunction(self.changeDirectory, resolveFilename(SCOPE_PLAYLIST)),
			"blue": boundFunction(self.changeDirectory, config.plugins.jukebox.musicFolder.value),
			"up": self["filelist"].up, "down": self["filelist"].down,
			"left": self["filelist"].pageUp, "right": self["filelist"].pageDown,
		}, -1)
		self.onLayoutFinish.append(self.updateTarget)

	def changeDirectory(self, directory):
		self["filelist"].changeDir(directory if isdir(directory) else "/media")
		self.updateTarget()

	def updateTarget(self):
		self["target"].setText(self["filelist"].getCurrentDirectory() or _("Select a folder"))

	def selectFile(self):
		if self["filelist"].canDescent():
			self["filelist"].descent()
			self.updateTarget()
		else:
			fileName = self["filelist"].getPath()
			if fileName:
				self.close(fileName)

	def green(self):
		directory = self["filelist"].getCurrentDirectory()
		if self.mode == "save" and directory:
			self.session.openWithCallback(self.nameEntered, VirtualKeyBoard, title=_("Playlist filename (.e2pls or .m3u)"), text="Jukebox.e2pls")
		elif self.mode == "add" and directory:
			self.close(directory)
		else:
			self.selectFile()

	def nameEntered(self, name):
		if name is not None:
			try:
				self.close(PlaylistFile.destination(self["filelist"].getCurrentDirectory(), name))
			except ValueError:
				self.session.open(MessageBox, _("Enter a filename ending in .e2pls or .m3u, without folder separators."), type=MessageBox.TYPE_ERROR)


class JukeboxLibrary(Screen):
	def __init__(self, session, serviceList, owner=None):
		self.session = session
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxLibrary")
		self["list"] = LibraryList()
		self["list"].connectSelChanged(self.lcdUpdate)
		self["actions"] = ActionMap(
			["WizardActions", "DirectionActions", "ColorActions", "MenuActions"],
			{
				"ok": self.ok,
				"back": self.closing,
				"red": self.closeOrRoot,
				"green": self.keyGreen,
				"yellow": self.keyYellow,
				"blue": self.keyBlue,
				"menu": self.keyMenu,
				"info": self.keyInfo,
			},
			-1,
		)

		self["actions2"] = NumberActionMap(
			["InputActions"],
			{
				"0": self.keyNumber,
			},
			-1,
		)

		self.onLayoutFinish.append(self.startRun)
		self.onShown.append(self.lcdUpdate)
		self.onClose.append(self.onCloseCallback)

		self.serviceList = serviceList
		self.owner = owner
		self.currentService = owner.currentService if owner else self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if owner is None:
			self.session.nav.stopService()

		self.mode = 0
		self.mainMenuList = []
		self.cacheList = []
		self.lastAction = None
		self.player = owner.player if owner else None
		if owner:
			owner.player = None
		if self.player:
			self.player.browser = self
		self["libraryActions"] = ActionMap(["JukeboxLibraryActions"], {"openLibrary": self.closing}, -2)

		self["key_red"] = StaticText("")
		self["key_green"] = StaticText("")
		self["key_yellow"] = StaticText("")
		self["key_blue"] = StaticText("")
		self["headertext"] = Label(_("Jukebox Library"))
		self["menuHint"] = StaticText(_("MENU: Options"))

		self.startPlayerScreenTimer = eTimer()
		self.startPlayerScreenTimer.timeout.get().append(self.keyInfo)

	def getPlaylists(self):
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				playlists = []
				cursor.execute("select playlist_id,playlist_text from playlists order by playlist_text;")
				for row in cursor:
					playlists.append((row[1], row[0]))
				cursor.close()
				return playlists
			else:
				return []

	def sqlCommand(self, sqlStatement, parameters=()):
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				cursor.execute(sqlStatement, parameters)
				cursor.close()
				connection.commit()

	def clearCache(self):
		for items in self.cacheList:
			items.cache = False
			items.entries = []
			items.headerText = ""

	def getCurrentSelection(self):
		selection = None
		try:
			selection = self["list"].l.getCurrentSelection()[0]
		except Exception:
			pass
		return selection

	def addListToPlaylistConfirmed(self, callback, answer):
		if answer:
			playlists = self.getPlaylists()
			if playlists:
				self.session.openWithCallback(callback, ChoiceBox, list=playlists)
			else:
				self.session.openWithCallback(self.createPlaylistConfirmed, MessageBox, _("There are no playlists defined.\nDo you want to create a new playlist?"))

	def keyMenu(self):
		self.startPlayerScreenTimer.stop()
		options = [
			(_("Configuration"), self.openSetup),
			(_("Search in music library"), self.searchLibrary),
		]
		options.extend(((_("Scan path for music files and add them to database"), self.scanDir),))
		if self.mode != 1:
			options.extend(((_("Create new playlist"), self.createPlaylist),))
		if self["list"].getDisplaySongMode():
			selection = self.getCurrentSelection()
			if selection and selection.songId:
				options.insert(0, (_("Edit database entry"), boundFunction(self.editSong, False)))
				if selection.fileName.lower().endswith(".mp3"):
					options.insert(0, (_("Edit MP3 tags"), boundFunction(self.editSong, True)))
				options.append((_("Lyrics / Tags"), self.showSongTags))
			if self.mode == 2:
				options.extend(((_("Delete song from current playlist"), self.deleteSongFromPlaylist),))
			else:
				options.extend(((_("Add selected song to a playlist"), self.addSongToPlaylist),))
				if self.mode == 18:
					options.extend(((_("Add all songs from selected album to a playlist"), self.addAlbumToPlaylist),))
				elif self.mode == 19:
					options.extend(((_("Add all songs from selected artist to a playlist"), self.addArtistToPlaylist),))
				options.extend(((_("Delete song from database"), self.deleteSongFromDatabase),))
			options.extend(((_("Clear current songlist and play selected entry"), self.stopPlayingAndAppendFileToSongList),))
			options.extend(((_("Append file to current playing songlist"), self.appendFileToSongList),))
			if self.player is not None and self.player.songList:
				options.extend(((_("Insert file to current playing songlist and play next"), self.insertFileToSongList),))
		else:
			if self.mode == 1:
				options.extend(((_("Delete selected playlist"), self.deletePlaylist),))
			elif self.mode == 4:
				options.extend(((_("Add all songs from selected artist to a playlist"), self.addArtistToPlaylist),))
			elif self.mode == 5 or self.mode == 7:
				options.extend(((_("Add all songs from selected album to a playlist"), self.addAlbumToPlaylist),))
			elif self.mode == 13:
				options.extend(((_("Add all songs from selected genre to a playlist"), self.addGenreToPlaylist),))
		self.session.openWithCallback(self.menuCallback, ChoiceBox, list=options)

	def showSongTags(self):
		selection = self.getCurrentSelection()
		if selection and selection.songId:
			self.startPlayerScreenTimer.stop()
			self.session.open(JukeboxTags, selection)

	def editSong(self, fileTags):
		selection = self.getCurrentSelection()
		if selection and selection.songId:
			self.startPlayerScreenTimer.stop()
			if libraryScanner.isRunning:
				self.session.open(MessageBox, _("Wait for the library scan to finish."), type=MessageBox.TYPE_INFO)
			else:
				try:
					self.session.openWithCallback(self.songEdited, JukeboxTagEditor, selection, fileTags)
				except (OSError, DatabaseError, ValueError) as error:
					self.session.open(MessageBox, _("Cannot open editor: %s") % error, type=MessageBox.TYPE_ERROR)

	def songEdited(self, values=None):
		if values:
			self.clearCache()
			if self.player:
				for song in self.player.songList:
					if song[0].songId == values["songId"]:
						for field in ("title", "artist", "album", "genre", "track", "date"):
							setattr(song[0], field, values[field])
			if self.lastAction:
				self.lastAction.callback(**self.lastAction.arguments)
			else:
				self.keyRed()

	def menuCallback(self, selection):
		selection and selection[1]()

	def scanDir(self):
		self.session.openWithCallback(self.pathSelected, JukeboxPath, "/media/")

	def pathSelected(self, selectedPath):
		if selectedPath is not None:
			self.session.openWithCallback(self.filesAdded, JukeboxScanner, selectedPath)

	def filesAdded(self):
		if libraryScanner.isRunning:
			self.close()
		else:
			self.keyRed()

	def addGenreToPlaylist(self):
		self.session.openWithCallback(
			boundFunction(self.addListToPlaylistConfirmed, self.addGenreToPlaylistConfirmedCallback),
			MessageBox,
			_("Do you really want to add all songs from that genre to a playlist?"),
		)

	def addGenreToPlaylistConfirmedCallback(self, playlistChoice):
		if playlistChoice:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand(
					"INSERT INTO Playlist_Songs (playlist_id,song_id) select ?, song_id from songs where genre_id=? order by album_id,tracknumber,title,filename;",
					(playlistChoice[1], selection.genreId),
				)
				self.clearCache()

	def addArtistToPlaylist(self):
		self.session.openWithCallback(
			boundFunction(self.addListToPlaylistConfirmed, self.addArtistToPlaylistConfirmedCallback),
			MessageBox,
			_("Do you really want to add all songs from that artist to a playlist?"),
		)

	def addArtistToPlaylistConfirmedCallback(self, playlistChoice):
		if playlistChoice:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand(
					"INSERT INTO Playlist_Songs (playlist_id,song_id) select ?, song_id from songs where artist_id=? order by album_id,tracknumber,title,filename;",
					(playlistChoice[1], selection.artistId),
				)
				self.clearCache()

	def addAlbumToPlaylist(self):
		self.session.openWithCallback(
			boundFunction(self.addListToPlaylistConfirmed, self.addAlbumToPlaylistConfirmedCallback),
			MessageBox,
			_("Do you really want to add all songs from that album to a playlist?"),
		)

	def addAlbumToPlaylistConfirmedCallback(self, playlistChoice):
		if playlistChoice:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand(
					"INSERT INTO Playlist_Songs (playlist_id,song_id) select ?, song_id from songs where album_id=? order by tracknumber,title,filename;",
					(playlistChoice[1], selection.albumId),
				)
				self.clearCache()

	def deletePlaylist(self):
		self.session.openWithCallback(self.deletePlaylistConfirmed, MessageBox, _("Do you really want to delete the current playlist?"))

	def deletePlaylistConfirmed(self, answer):
		if answer:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand("delete from playlist_songs where playlist_id = ?", (selection.playlistId,))
				self.sqlCommand("delete from playlists where playlist_id = ?", (selection.playlistId,))
				self["list"].removeItem(self["list"].getCurrentIndex())
				self.clearCache()

	def deleteSongFromPlaylist(self):
		self.session.openWithCallback(self.deleteSongFromPlaylistConfirmed, MessageBox, _("Do you really want to delete that song the current playlist?"))

	def deleteSongFromPlaylistConfirmed(self, answer):
		if answer:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand("delete from playlist_songs where song_id = ? AND playlist_id = ?", (selection.songId, selection.playlistId))
				self["list"].removeItem(self["list"].getCurrentIndex())
				self.clearCache()

	def deleteSongFromDatabase(self):
		self.session.openWithCallback(self.deleteSongFromDatabaseConfirmed, MessageBox, _("Do you really want to delete that song from the database?"))

	def deleteSongFromDatabaseConfirmed(self, answer):
		if answer:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand("delete from playlist_songs where song_id = ?", (selection.songId,))
				self.sqlCommand("delete from songs where song_id = ?", (selection.songId,))
				self["list"].removeItem(self["list"].getCurrentIndex())
				self.clearCache()

	def addSongToPlaylist(self):
		playlists = self.getPlaylists()
		if playlists:
			self.session.openWithCallback(self.addSongToPlaylistCallback, ChoiceBox, list=playlists)
		else:
			self.session.openWithCallback(self.createPlaylistConfirmed, MessageBox, _("There are no playlists defined.\nDo you want to create a new playlist?"))

	def createPlaylistConfirmed(self, val):
		if val:
			self.createPlaylist()

	def addSongToPlaylistCallback(self, playlistChoice):
		if playlistChoice:
			selection = self.getCurrentSelection()
			if selection:
				self.sqlCommand("INSERT INTO Playlist_Songs (playlist_id,song_id) VALUES(?,?);", (playlistChoice[1], selection.songId))
				self.clearCache()

	def createPlaylist(self):
		self.session.openWithCallback(self.createPlaylistFinished, VirtualKeyBoard, title=_("Enter name for playlist"))

	def createPlaylistFinished(self, text=None):
		if text:
			self.sqlCommand("INSERT INTO Playlists (playlist_text) VALUES (?);", (text,))
			self.clearCache()
			self.keyMenu()

	def searchLibrary(self):
		options = [
			(_("search for title"), 1),
			(_("search for artist"), 2),
			(_("search for album"), 3),
			(_("search in all of them"), 4),
		]
		self.session.openWithCallback(self.enterSearchText, ChoiceBox, list=options)

	def enterSearchText(self, selection):
		if selection:
			self.session.openWithCallback(boundFunction(self.enterSearchTextFinished, selection[1]), VirtualKeyBoard, title=_("Enter search text"))

	def enterSearchTextFinished(self, searchType, searchText=None):
		if searchText:
			search = f"%{searchText}%"
			parameters = (search,)
			if searchType == 1:
				whereClause = "where title like ?"
				text = _('Search results for "%s" in all titles') % searchText
			elif searchType == 2:
				whereClause = "where artists.artist like ?"
				text = _('Search results for "%s" in all artists') % searchText
			elif searchType == 3:
				whereClause = "where album_text like ?"
				text = _('Search results for "%s" in all albums') % searchText
			else:
				whereClause = "where (title like ? or artists.artist like ? or album_text like ?)"
				parameters = (search, search, search)
				text = _('Search results for "%s" in title, artist or album') % searchText
			self.setButtons(red=True, yellow=True, blue=True)
			previousMode = self.mode
			self.mode = 20
			self["list"].setMode(self.mode)
			self.buildSearchSongList(whereClause, text, previousMode, True, parameters)

	def keyNumber(self, number):
		if number == 0 and self.mode != 0:
			self["list"].moveToIndex(0)
			self.ok()

	def ok(self):
		selection = self.getCurrentSelection()
		if selection is None:
			return
		if selection.mode == 99:
			self.keyGreen()
		else:
			self.mode = selection.mode
			self["list"].setMode(self.mode)
			if selection.navigator and len(self.cacheList) > 0:
				cache = self.cacheList.pop()
			else:
				cache = LibraryViewState(cache=False, index=-1)
			if selection.navigator:
				self["headertext"].setText(cache.headerText)
				if cache.cache:
					self["list"].setList(cache.entries)
					self.lastAction = LibraryAction(callback=cache.action.callback, arguments=cache.action.arguments)
				else:
					cache.action.callback(**cache.action.arguments)
				self["list"].moveToIndex(cache.index)
			if self.mode == 0:
				self.setButtons()
				if not selection.navigator:
					self.buildMainMenuList()
			elif self.mode == 1:
				self.setButtons(red=True)
				if not selection.navigator:
					self.buildPlaylistList(addToCache=True)
			elif self.mode == 2:
				self.setButtons(red=True, green=True, yellow=True, blue=True)
				if not selection.navigator:
					self.buildPlaylistSongList(playlistId=selection.playlistId, addToCache=True)
			elif self.mode == 4:
				self.setButtons(red=True)
				if not selection.navigator:
					self.buildArtistList(addToCache=True)
			elif self.mode == 5:
				self.setButtons(red=True)
				if not selection.navigator:
					self.buildArtistAlbumList(selection.artistId, addToCache=True)
			elif self.mode == 6:
				self.setButtons(red=True, green=True, yellow=True)
				if not selection.navigator:
					self.buildAlbumSongList(albumId=selection.albumId, mode=5, addToCache=True)
			elif self.mode == 7:
				self.setButtons(red=True)
				if not selection.navigator:
					self.buildAlbumList(addToCache=True)
			elif self.mode == 8:
				self.setButtons(red=True, green=True, yellow=True)
				if not selection.navigator:
					self.buildAlbumSongList(albumId=selection.albumId, mode=7, addToCache=True)
			elif self.mode == 10:
				self.setButtons(red=True, green=True, yellow=True, blue=True)
				if not selection.navigator:
					self.buildSongList(addToCache=True)
			elif self.mode == 13:
				self.setButtons(red=True)
				if not selection.navigator:
					self.buildGenreList(addToCache=True)
			elif self.mode == 14:
				self.setButtons(red=True, green=True, yellow=True, blue=True)
				if not selection.navigator:
					self.buildGenreSongList(genreId=selection.genreId, addToCache=True)
			elif self.mode == 18 or self.mode == 19:
				if self.mode == 18:
					self.setButtons(red=True, green=True, yellow=True)
				if self.mode == 19:
					self.setButtons(red=True, green=True, blue=True)
				if not selection.navigator:
					self.keyRed()  # back to main menu --> normally that can not be happened
			elif self.mode == 20:
				self.setButtons(red=True, green=True, yellow=True, blue=True)
				if not selection.navigator:
					self.keyRed()  # back to main menu --> normally that can not be happened

	def buildPlaylistList(self, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildPlaylistList, arguments=arguments)
		self["headertext"].setText(_("Playlists"))
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				playlistList = []
				playlistList.append((MusicItem(text=_("[back]"), mode=0, navigator=True),))
				cursor.execute(
					"select playlists.playlist_id, playlist_text, count(Playlist_Songs.playlist_id) from playlists left outer join Playlist_Songs on playlists.playlist_id = Playlist_Songs.playlist_id group by playlists.playlist_id order by playlists.playlist_text;"
				)
				for row in cursor:
					playlistList.append((MusicItem(text=f"{row[1]} ({row[2]:d})", mode=2, playlistId=row[0]),))
				cursor.close()
				self["list"].setList(playlistList)
				if len(playlistList) > 1:
					self["list"].moveToIndex(1)

	def buildPlaylistSongList(self, playlistId, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["playlistId"] = playlistId
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildPlaylistSongList, arguments=arguments)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				playlistSongList = []
				playlistSongList.append((MusicItem(text=_("[back]"), mode=1, navigator=True),))
				cursor.execute(
					"select songs.song_id, title, artists.artist, filename, songs.artist_id, bitrate, length, genre_text, track, date, album_text, songs.Album_id from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id inner join playlist_songs on songs.song_id = playlist_songs.song_id where playlist_songs.playlist_id =  ? order by playlist_songs.id;",
					(playlistId,),
				)
				for row in cursor:
					playlistSongList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								artistId=row[4],
								bitrate=row[5],
								length=row[6],
								genre=row[7],
								track=row[8],
								date=row[9],
								album=row[10],
								albumId=row[11],
								playlistId=playlistId,
							),
						)
					)
				cursor.execute("SELECT playlist_text from playlists where playlist_id = ?;", (playlistId,))
				row = cursor.fetchone()
				self["headertext"].setText(_("Playlist (%s) -> Song List") % row[0])
				cursor.close()
				self["list"].setList(playlistSongList)
				if len(playlistSongList) > 1:
					self["list"].moveToIndex(1)

	def buildGenreList(self, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildGenreList, arguments=arguments)
		self["headertext"].setText(_("Genre List"))
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				genreList = []
				genreList.append((MusicItem(text=_("[back]"), mode=0, navigator=True),))
				cursor.execute(
					"select Genre.genre_id,Genre.Genre_text, count(*) from songs inner join Genre on songs.genre_id = Genre.Genre_id group by songs.Genre_id order by Genre.Genre_text;"
				)
				for row in cursor:
					genreList.append((MusicItem(text=f"{row[1]} ({row[2]:d})", mode=14, genreId=row[0]),))
				cursor.close()
				self["list"].setList(genreList)
				if len(genreList) > 1:
					self["list"].moveToIndex(1)

	def buildGenreSongList(self, genreId, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["genreId"] = genreId
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildGenreSongList, arguments=arguments)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				genreSongList = []
				genreSongList.append((MusicItem(text=_("[back]"), mode=13, navigator=True),))
				cursor.execute(
					"select song_id, title, artists.artist, filename, songs.artist_id, bitrate, length, genre_text, track, date, album_text, songs.Album_id from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id where songs.genre_id = ? order by title, filename;",
					(genreId,),
				)
				for row in cursor:
					genreSongList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								artistId=row[4],
								bitrate=row[5],
								length=row[6],
								genre=row[7],
								track=row[8],
								date=row[9],
								album=row[10],
								albumId=row[11],
								genreId=genreId,
							),
						)
					)
				cursor.execute("SELECT genre_text from genre where genre_ID = ?;", (genreId,))
				row = cursor.fetchone()
				self["headertext"].setText(_("Genre (%s) -> Song List") % row[0])
				cursor.close()
				self["list"].setList(genreSongList)
				if len(genreSongList) > 1:
					self["list"].moveToIndex(1)

	def setButtons(self, red=False, green=False, yellow=False, blue=False):
		if red:
			self["key_red"].setText(_("Main Menu"))
		else:
			self["key_red"].setText("")
		if green:
			self["key_green"].setText(_("Play"))
		else:
			self["key_green"].setText("")
		if yellow:
			self["key_yellow"].setText(_("All Artists"))
		else:
			self["key_yellow"].setText("")
		if blue:
			self["key_blue"].setText(_("Show Album"))
		else:
			self["key_blue"].setText("")

	def keyInfo(self):
		self.startPlayerScreenTimer.stop()
		if self.player is not None:
			if self.player.songList:
				self.session.execDialog(self.player)

	def keyGreen(self):
		if self.mode == 0:
			self.ok()
			return
		try:
			selection = self["list"].l.getCurrentSelection()[0]
		except Exception:
			selection = None
		if selection is None:
			return
		if selection.songId != 0:
			if self.player is not None:
				self.player.doClose()
				self.player = None
			self.startPlayerScreenTimer.stop()
			self.player = self.session.instantiateDialog(JukeboxPlayer, self["list"].getList()[1:], self["list"].getCurrentIndex() - 1, True, self.currentService, self.serviceList, self)
			self.session.execDialog(self.player)

	def closeOrRoot(self):
		if self.mode == 0:
			self.closing()
		else:
			self.keyRed()

	def keyRed(self):
		self.cacheList = []
		self.setButtons()
		self.mode = 0
		self["list"].setMode(self.mode)
		self.buildMainMenuList()

	def keyYellow(self):
		if self.mode == 0:
			self.openSetup()
			return
		try:
			selection = self["list"].l.getCurrentSelection()[0]
		except Exception:
			return
		if selection.artistId != 0:
			previousMode = self.mode
			self.mode = 19
			self.setButtons(red=True, green=True, blue=True)
			self["list"].setMode(self.mode)
			self.buildArtistSongList(artistId=selection.artistId, mode=previousMode, addToCache=True)

	def keyBlue(self):
		if self.mode == 0:
			self.scanDir()
			return
		try:
			selection = self["list"].l.getCurrentSelection()[0]
		except Exception:
			return
		if selection.albumId != 0:
			self.setButtons(red=True, green=True, yellow=True)
			previousMode = self.mode
			self.mode = 18
			self["list"].setMode(self.mode)
			self.buildAlbumSongList(albumId=selection.albumId, mode=previousMode, addToCache=True)

	def buildSongList(self, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildSongList, arguments=arguments)
		self["headertext"].setText(_("All Songs"))
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				songList = []
				songList.append((MusicItem(text=_("[back]"), mode=0, navigator=True),))
				cursor.execute(
					"select song_id, title, artists.artist, filename, songs.artist_id, bitrate, length, genre_text, track, date, album_text, songs.Album_id from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id order by title, filename;"
				)
				for row in cursor:
					songList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								artistId=row[4],
								bitrate=row[5],
								length=row[6],
								genre=row[7],
								track=row[8],
								date=row[9],
								album=row[10],
								albumId=row[11],
							),
						)
					)
				cursor.close()
				self["list"].setList(songList)
				if len(songList) > 1:
					self["list"].moveToIndex(1)

	def buildSearchSongList(self, whereClause, headerText, mode, addToCache, parameters=()):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["whereClause"] = whereClause
		arguments["parameters"] = parameters
		arguments["headerText"] = headerText
		arguments["mode"] = mode
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildSearchSongList, arguments=arguments)
		self["headertext"].setText(headerText)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				songList = []
				songList.append((MusicItem(text=_("[back]"), mode=mode, navigator=True),))
				cursor.execute(
					f"select song_id, title, artists.artist, filename, songs.artist_id, bitrate, length, genre_text, track, date, album_text, songs.Album_id from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id {whereClause} order by title, filename;",
					parameters,
				)
				for row in cursor:
					songList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								artistId=row[4],
								bitrate=row[5],
								length=row[6],
								genre=row[7],
								track=row[8],
								date=row[9],
								album=row[10],
								albumId=row[11],
							),
						)
					)
				cursor.close()
				self["list"].setList(songList)
				if len(songList) > 1:
					self["list"].moveToIndex(1)

	def buildArtistSongList(self, artistId, mode, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["artistId"] = artistId
		arguments["mode"] = mode
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildArtistSongList, arguments=arguments)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				artistSongList = []
				artistSongList.append((MusicItem(text=_("[back]"), mode=mode, navigator=True),))
				cursor.execute(
					"select song_id, title, artists.artist, filename, bitrate, length, genre_text, track, date, album_text, songs.Album_id from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id where songs.artist_id = ? order by Album.album_text, tracknumber, filename;",
					(artistId,),
				)
				for row in cursor:
					artistSongList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								bitrate=row[4],
								length=row[5],
								genre=row[6],
								track=row[7],
								date=row[8],
								album=row[9],
								albumId=row[10],
								artistId=artistId,
							),
						)
					)
				cursor.execute("SELECT artist from artists where artist_ID = ?;", (artistId,))
				row = cursor.fetchone()
				self["headertext"].setText(_("Artist (%s) -> Song List") % row[0])
				cursor.close()
				self["list"].setList(artistSongList)
				if len(artistSongList) > 1:
					self["list"].moveToIndex(1)

	def buildAlbumSongList(self, albumId, mode, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["albumId"] = albumId
		arguments["mode"] = mode
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildAlbumSongList, arguments=arguments)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				albumSongList = []
				albumSongList.append((MusicItem(text=_("[back]"), mode=mode, navigator=True),))
				cursor.execute(
					"select song_id, title, artists.artist, filename, songs.artist_id, bitrate, length, genre_text, track, date, album_text from songs inner join artists on songs.artist_id = artists.artist_id inner join Album on songs.Album_id = Album.Album_id inner join genre on songs.genre_id = genre.genre_id where songs.album_id = ? order by tracknumber, filename;",
					(albumId,),
				)
				for row in cursor:
					albumSongList.append(
						(
							MusicItem(
								mode=99,
								songId=row[0],
								title=row[1],
								artist=row[2],
								fileName=row[3],
								artistId=row[4],
								bitrate=row[5],
								length=row[6],
								genre=row[7],
								track=row[8],
								date=row[9],
								album=row[10],
								albumId=albumId,
							),
						)
					)
				cursor.execute("SELECT album_text from album where album_ID = ?;", (albumId,))
				row = cursor.fetchone()
				self["headertext"].setText(_("Album (%s) -> Song List") % row[0])
				cursor.close()
				self["list"].setList(albumSongList)
				if len(albumSongList) > 1:
					self["list"].moveToIndex(1)

	def buildMainMenuList(self, addToCache=True):
		self["key_red"].setText(_("Close"))
		self["key_green"].setText(_("Open"))
		self["key_yellow"].setText(_("Settings"))
		self["key_blue"].setText(_("Scan music"))
		arguments = {}
		arguments["addToCache"] = True
		self.lastAction = LibraryAction(callback=self.buildMainMenuList, arguments=arguments)
		self["headertext"].setText(_("Jukebox Library"))
		mainMenuList = []
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				# 1. Playlists
				cursor.execute("SELECT COUNT (*) FROM playlists;")
				row = cursor.fetchone()
				mainMenuList.append((MusicItem(text=_("Playlists (%d)") % row[0], mode=1),))
				# 2. Artists
				cursor.execute("SELECT COUNT (*) FROM artists;")
				row = cursor.fetchone()
				mainMenuList.append((MusicItem(text=_("Artists (%d)") % row[0], mode=4),))
				# 3. Albums
				cursor.execute("SELECT COUNT (DISTINCT album_text) FROM album;")
				row = cursor.fetchone()
				mainMenuList.append((MusicItem(text=_("Albums (%d)") % row[0], mode=7),))
				# 4. Songs
				cursor.execute("SELECT COUNT (*) FROM songs;")
				row = cursor.fetchone()
				mainMenuList.append((MusicItem(text=_("Songs (%d)") % row[0], mode=10),))
				# 5. Genres
				cursor.execute("SELECT COUNT (*) FROM genre;")
				row = cursor.fetchone()
				mainMenuList.append((MusicItem(text=_("Genres (%d)") % row[0], mode=13),))
				cursor.close()
				self["list"].setList(mainMenuList)
				self["list"].moveToIndex(0)

	def buildArtistList(self, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildArtistList, arguments=arguments)
		self["headertext"].setText(_("Artists List"))
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				artistList = []
				artistList.append((MusicItem(text=_("[back]"), mode=0, navigator=True),))
				cursor.execute(
					"SELECT artists.artist_id,artists.artist, count (distinct album.album_text) FROM songs INNER JOIN artists ON songs.artist_id = artists.artist_id inner join album on songs.album_id =  album.album_id GROUP BY songs.artist_id ORDER BY artists.artist;"
				)
				for row in cursor:
					artistList.append((MusicItem(text=f"{row[1]} ({row[2]:d})", mode=5, artistId=row[0]),))
				cursor.close()
				self["list"].setList(artistList)

	def buildArtistAlbumList(self, artistId, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["artistId"] = artistId
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildArtistAlbumList, arguments=arguments)
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				albumArtistList = []
				albumArtistList.append((MusicItem(text=_("[back]"), mode=4, navigator=True),))
				cursor.execute(
					"select Album.Album_id,Album.Album_text from songs inner join Album on songs.Album_id = Album.Album_id where songs.artist_id = ? group by songs.Album_id order by Album.Album_text;",
					(artistId,),
				)
				for row in cursor:
					cursor2 = connection.cursor()
					cursor2.execute("select count(song_id) from songs where album_id = ?;", (row[0],))
					row2 = cursor2.fetchone()
					albumArtistList.append((MusicItem(text=f"{row[1]} ({row2[0]:d})", mode=6, albumId=row[0], artistId=artistId),))
					cursor2.close()
				cursor.execute("SELECT artist from artists where artist_ID = ?;", (artistId,))
				row = cursor.fetchone()
				self["headertext"].setText(_("Artist (%s) -> Album List") % row[0])
				cursor.close()
				self["list"].setList(albumArtistList)
				if len(albumArtistList) > 1:
					self["list"].moveToIndex(1)

	def buildAlbumList(self, addToCache):
		if addToCache:
			self.cacheList.append(
				LibraryViewState(index=self["list"].getCurrentIndex(), entries=self["list"].getList(), headerText=self["headertext"].getText(), action=self.lastAction)
			)
		arguments = {}
		arguments["addToCache"] = False
		self.lastAction = LibraryAction(callback=self.buildAlbumList, arguments=arguments)
		self["headertext"].setText(_("Albums List"))
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				albumList = []
				albumList.append((MusicItem(text=_("[back]"), mode=0, navigator=True),))
				cursor.execute(
					"select Album.Album_id,Album.Album_text, count(*) from songs inner join Album on songs.Album_id = Album.Album_id group by songs.Album_id order by Album.Album_text;"
				)
				for row in cursor:
					albumList.append((MusicItem(text=f"{row[1]} ({row[2]:d})", mode=8, albumId=row[0]),))
				cursor.close()
				self["list"].setList(albumList)
				if len(albumList) > 1:
					self["list"].moveToIndex(1)

	def startRun(self):
		if libraryScanner.isRunning:
			self.showScanner = eTimer()
			self.showScanner.callback.append(self.showScannerCallback)
			self.showScanner.start(0, 1)
		else:
			if self.owner is None and config.plugins.jukebox.resumePlaylist.value:
				self.startPlayerTimer = eTimer()
				self.startPlayerTimer.callback.append(self.startPlayerTimerCallback)
				self.startPlayerTimer.start(0, 1)
			self.mode = 0
			self["list"].setMode(self.mode)
			self.buildMainMenuList()

	def showScannerCallback(self):
		self.session.openWithCallback(self.filesAdded, JukeboxScanner, None)

	def startPlayerTimerCallback(self):
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				libraryMode = False
				songList = []
				cursor.execute("select song_id, filename, title, artist, album, genre, bitrate, length,  track, date, PTS from CurrentSongList;")
				for row in cursor:
					songList.append(
						(
							MusicItem(
								songId=row[0],
								text=basename(row[1]),
								fileName=row[1],
								title=row[2],
								artist=row[3],
								album=row[4],
								genre=row[5],
								bitrate=row[6],
								length=row[7],
								track=row[8],
								date=row[9],
								cuePts=row[10],
								formatMetadata=False,
							),
						)
					)
					if row[0] != 0:
						libraryMode = True
				cursor.close()
				if self.player is not None:
					self.player.doClose()
					self.player = None
				self.startPlayerScreenTimer.stop()
				count = len(songList)
				if count:
					# just to be sure, check the index , it's critical
					index = config.plugins.jukebox.lastSongIndex.value
					if index < 0 or index >= count:
						index = 0
					self.player = self.session.instantiateDialog(JukeboxPlayer, songList, index, libraryMode, self.currentService, self.serviceList, self)
					self.session.execDialog(self.player)

	def openSetup(self):
		self.startPlayerScreenTimer.stop()
		self.session.openWithCallback(self.setupFinished, JukeboxSetup, True)

	def setupFinished(self, result=None):
		if result:
			self.keyRed()

	def stopPlayingAndAppendFileToSongList(self):
		self.startPlayerScreenTimer.stop()
		if self.player is not None:
			self.player.doClose()
			self.player = None
		self.appendFileToSongList()
		self.startPlayerScreenTimer.start(JukeboxPlayer.RETURN_DELAY, True)

	def appendFileToSongList(self):
		songList = []
		playerAvailable = self.player is not None and self.player.songList
		selection = self.getCurrentSelection()
		if selection:
			if playerAvailable:
				self.player.songList.append((selection,))
				self.player.originalSongList.append((selection,))
			else:
				songList.append((selection,))
			if not playerAvailable:
				if self.player is not None:
					self.player.doClose()
					self.player = None
				self.player = self.session.instantiateDialog(JukeboxPlayer, songList, 0, True, self.currentService, self.serviceList, self)
				self.player.playSong(self.player.songList[self.player.currentIndex][0].fileName)
				self.player["coverArt"].onShow()
				self.player.playbackStarted = True
			else:
				self.player["nextTitle"].setText(self.player.getNextTitle())
				self.session.open(MessageBox, _("%s\nappended to songlist") % selection.title, type=MessageBox.TYPE_INFO, timeout=3)

	def insertFileToSongList(self):
		selection = self.getCurrentSelection()
		if selection:
			if self.player is not None and self.player.songList:
				index = self.player.currentIndex
				self.player.songList.insert(index + 1, (selection,))
				self.player.originalSongList.insert(index + 1, (selection,))
				self.player["nextTitle"].setText(self.player.getNextTitle())
				self.session.open(MessageBox, _("%s\ninserted and will be played as next song") % selection.title, type=MessageBox.TYPE_INFO, timeout=3)
			else:
				self.appendFileToSongList()

	def closing(self):
		self.close()

	def onCloseCallback(self):
		self.startPlayerScreenTimer.stop()
		if self.owner is not None:
			self.owner.player = self.player
			if self.player:
				self.player.browser = self.owner
			self.player = None
			return
		if self.player is not None:
			self.player.closePlayer()
			self.player.doClose()
			self.player = None
		if self.serviceList is None:
			self.session.nav.playService(self.currentService, adjust=False)
		else:
			current = ServiceReference(self.serviceList.getCurrentSelection())
			self.session.nav.playService(current.ref, adjust=False)

	def lcdUpdate(self):
		selection = self.getCurrentSelection()
		if self["list"].getDisplaySongMode() and selection and selection.songId:
			self["menuHint"].setText(_("MENU: MP3 tags / Database") if selection.fileName.lower().endswith(".mp3") else _("MENU: Edit database"))
		else:
			self["menuHint"].setText(_("MENU: Options"))
		self.startPlayerScreenTimer.start(JukeboxPlayer.RETURN_DELAY, True)
		entries = self["list"].getList()
		index = self["list"].getCurrentIndex()
		for offset, line in ((0, 1), (-1, 3), (1, 4)):
			entry = entries[(index + offset) % len(entries)][0] if entries else None
			self.summaries.setText((entry.title or entry.text) if entry else "", line)

	def createSummary(self):
		return JukeboxSummaryText


class LibraryList(GUIComponent):
	def buildEntry(self, item):
		width = self.l.getItemSize().width()
		height = self.songRowHeight if self.displaySongMode else self.rowHeight
		lineHeight = height // 3 if self.displaySongMode else height
		margin = max(2, int(4 * getSkinFactor()))
		alignment = RT_HALIGN_CENTER if item.navigator else RT_HALIGN_LEFT
		title = item.text if item.navigator else (item.title or item.text)
		if self.displaySongMode and not item.navigator and item.artist:
			title = f"{title} — {item.artist}"
		result = [None, (eListboxPythonMultiContent.TYPE_TEXT, margin, 0, width - margin * 2, lineHeight, 0, alignment | RT_VALIGN_CENTER, title)]
		if self.displaySongMode and not item.navigator:
			rightWidth = max(100, width // 4)
			leftWidth = width - rightWidth - margin * 3
			for row, left, right in ((1, f"{item.album}{item.date}", item.track), (2, item.genre, f"{item.length}  {item.bitrate}")):
				result.append((eListboxPythonMultiContent.TYPE_TEXT, margin, row * lineHeight, leftWidth, lineHeight, 1, RT_HALIGN_LEFT | RT_VALIGN_CENTER, left))
				result.append(
					(eListboxPythonMultiContent.TYPE_TEXT, width - rightWidth - margin, row * lineHeight, rightWidth, lineHeight, 1, RT_HALIGN_RIGHT | RT_VALIGN_CENTER, right)
				)
		return result

	def __init__(self):
		GUIComponent.__init__(self)
		self.l = eListboxPythonMultiContent()
		self.l.setBuildFunc(self.buildEntry)
		factor = getSkinFactor()
		for index, name, size in ((0, "JukeboxListFont0", 24), (1, "JukeboxListFont1", 21)):
			font = fonts.get(name, ("Regular", int(size * factor)))
			self.l.setFont(index, gFont(font[0], font[1]))
		self.rowHeight = int(40 * factor)
		self.songRowHeight = int(120 * factor)
		self.l.setItemHeight(self.rowHeight)
		self.onSelectionChanged = []
		self.mode = 0
		self.displaySongMode = False
		self.list = []
		self.itemCount = -1

	def applySkin(self, desktop, screen):
		attributes = []
		for attribute, value in self.skinAttributes or []:
			if attribute in ("font", "detailFont"):
				self.l.setFont(0 if attribute == "font" else 1, parseFont(value, screen.scale))
			elif attribute in ("itemHeight", "songItemHeight"):
				height = int(value) * screen.scale[1][0] // screen.scale[1][1]
				if attribute == "itemHeight":
					self.rowHeight = height
				else:
					self.songRowHeight = height
			else:
				attributes.append((attribute, value))
		self.skinAttributes = attributes
		self.l.setItemHeight(self.songRowHeight if self.displaySongMode else self.rowHeight)
		return GUIComponent.applySkin(self, desktop, screen)

	def connectSelChanged(self, callback):
		if callback not in self.onSelectionChanged:
			self.onSelectionChanged.append(callback)

	def disconnectSelChanged(self, callback):
		if callback in self.onSelectionChanged:
			self.onSelectionChanged.remove(callback)

	def selectionChanged(self):
		for x in self.onSelectionChanged:
			x()

	def getCurrent(self):
		currentSelection = self.l.getCurrentSelection()
		return currentSelection and currentSelection[0]

	GUI_WIDGET = eListbox

	def postWidgetCreate(self, instance):
		instance.setContent(self.l)
		instance.setWrapAround(True)
		instance.selectionChanged.get().append(self.selectionChanged)

	def preWidgetRemove(self, instance):
		instance.setContent(None)
		instance.selectionChanged.get().remove(self.selectionChanged)

	def moveToIndex(self, index):
		self.instance.moveSelectionTo(index)

	def getCurrentIndex(self):
		return self.instance.getCurrentIndex()

	currentIndex = property(getCurrentIndex, moveToIndex)
	currentSelection = property(getCurrent)

	def setList(self, entries):
		self.list = entries
		self.l.setList(entries)
		self.itemCount = len(self.list) - 1

	def getItemCount(self):
		return self.itemCount

	def getList(self):
		return self.list

	def removeItem(self, index):
		del self.list[index]
		self.l.entryRemoved(index)
		self.itemCount = len(self.list) - 1

	def getDisplaySongMode(self):
		return self.displaySongMode

	def setMode(self, mode):
		self.mode = mode
		if mode == 2 or mode == 6 or mode == 8 or mode == 10 or mode == 18 or mode == 19 or mode == 14 or mode == 20:
			self.displaySongMode = True
			self.l.setItemHeight(self.songRowHeight)
		else:
			self.displaySongMode = False
			self.l.setItemHeight(self.rowHeight)


class MusicFileList(FileList):
	def applySkin(self, desktop, screen):
		attributes = []
		font = "Regular;24"
		height = 40
		for attribute, value in self.skinAttributes or []:
			if attribute == "font":
				font = value
			elif attribute == "itemHeight":
				height = int(value)
			else:
				attributes.append((attribute, value))
		self.skinAttributes = attributes
		self.itemHeight = height * screen.scale[1][0] // screen.scale[1][1]
		self.l.setFont(0, parseFont(font, screen.scale))
		self.l.setItemHeight(self.itemHeight)
		result = FileList.applySkin(self, desktop, screen)
		margin = max(2, self.itemHeight // 8)
		self.iconX = self.iconY = margin
		self.iconW = self.iconH = self.itemHeight - margin * 2
		self.nameX = self.iconW + margin * 3
		self.nameY = 0
		self.nameW = self.instance.size().width() - self.nameX - margin * 3
		self.nameH = self.itemHeight
		folderIcon = LoadPixmap(join(dirname(__file__), "skin", "jukebox-folder.svg"))
		musicIcon = LoadPixmap(join(dirname(__file__), "skin", "jukebox-record.svg"))
		for name in self.extensionIcons:
			if name in ("link_arrow", "link_error", "lock_off", "lock_on"):
				continue
			self.extensionIcons[name] = folderIcon if name in ("storage", "parent", "current", "directory") else musicIcon
		self.setIconFont()
		self.refresh()
		return result


class CoverPixmap(Pixmap):
	def __init__(self):
		Pixmap.__init__(self)
		self.coverFileName = ""
		self.temporaryCover = ""
		self.embeddedData = None
		self.pendingCover = ""
		self.decoding = False
		self.decodedCover = ""
		self.decodingFileName = ""
		self.pictureDecoder = ePicLoad()
		self.pictureDecoder.PictureData.get().append(self.coverDecoded)

	def applySkin(self, desktop, screen):
		defaultCover = join(dirname(__file__), "skin", "cover-placeholder.png")
		for attribute, value in self.skinAttributes or []:
			if attribute == "pixmap":
				defaultCover = resolveFilename(SCOPE_GUISKIN, value)
		self.noCoverPixmap = LoadPixmap(defaultCover)
		return Pixmap.applySkin(self, desktop, screen)

	def onShow(self):
		Pixmap.onShow(self)
		if self.instance:
			self.pictureDecoder.setPara((self.instance.size().width(), self.instance.size().height(), 1, 1, False, 1, "#ff000000"))
			if self.coverFileName:
				self.showCoverFromFile(self.coverFileName)
			else:
				self.showDefaultCover()

	def coverDecoded(self, pictureInfo=None):
		# getData releases the decoder before another image can be queued.
		picture = self.pictureDecoder.getData()
		self.decoding = False
		if self.instance:
			if self.pendingCover:
				fileName, self.pendingCover = self.pendingCover, ""
				self.showCoverFromFile(fileName)
			else:
				if picture is not None and self.coverFileName:
					self.instance.setPixmap(picture)
					self.decodedCover = self.decodingFileName

	def updateCoverArt(self, path):
		fileName = AudioMetadata(path).coverFile()
		if fileName:
			self.showCoverFromFile(fileName)
		return bool(fileName)

	def showDefaultCover(self):
		self.coverFileName = ""
		self.decodedCover = ""
		self.pendingCover = ""
		if self.instance:
			self.instance.setPixmap(self.noCoverPixmap)

	def showCoverFromFile(self, fileName):
		self.coverFileName = fileName
		if self.instance and isfile(fileName):
			if self.decoding:
				self.pendingCover = fileName if self.decodingFileName != fileName else ""
			elif self.decodedCover != fileName:
				self.decodingFileName = fileName
				self.pictureDecoder.setPara((self.instance.size().width(), self.instance.size().height(), 1, 1, False, 1, "#ff000000"))
				self.decoding = self.pictureDecoder.startDecode(fileName) == 0

	def showEmbeddedCover(self, data):
		if self.temporaryCover and self.embeddedData == data:
			self.showCoverFromFile(self.temporaryCover)
			return
		if self.temporaryCover and isfile(self.temporaryCover):
			unlink(self.temporaryCover)
		self.embeddedData = data
		with NamedTemporaryFile(prefix="jukebox-cover-", suffix=".jpg", delete=False) as handle:
			handle.write(data)
			self.temporaryCover = handle.name
		self.showCoverFromFile(self.temporaryCover)

	def embeddedCoverArt(self):
		if isfile("/tmp/.id3coverart"):
			self.showCoverFromFile("/tmp/.id3coverart")

	def preWidgetRemove(self, instance):
		self.pictureDecoder.PictureData.get().remove(self.coverDecoded)
		if self.temporaryCover and isfile(self.temporaryCover):
			unlink(self.temporaryCover)
		Pixmap.preWidgetRemove(self, instance)


class JukeboxPath(Screen):
	def __init__(self, session, initialDirectory):
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxPath")
		self.setTitle(_("Select path"))
		inhibitDirs = ["/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr", "/var"]
		inhibitMounts = []
		self["filelist"] = MusicFileList(initialDirectory, showDirectories=True, showFiles=False, inhibitMounts=inhibitMounts, inhibitDirs=inhibitDirs)
		self["target"] = Label()
		self["actions"] = ActionMap(
			["WizardActions", "DirectionActions", "ColorActions", "MenuActions"],
			{"back": self.cancel, "left": self.left, "right": self.right, "up": self.up, "down": self.down, "ok": self.ok, "green": self.green, "red": self.cancel},
			-1,
		)
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("OK"))

	def cancel(self):
		self.close(None)

	def green(self):
		self.close(self["filelist"].getPath() or self["filelist"].getCurrentDirectory())

	def up(self):
		self["filelist"].up()
		self.updateTarget()

	def down(self):
		self["filelist"].down()
		self.updateTarget()

	def left(self):
		self["filelist"].pageUp()
		self.updateTarget()

	def right(self):
		self["filelist"].pageDown()
		self.updateTarget()

	def ok(self):
		if self["filelist"].canDescent():
			self["filelist"].descent()
			self.updateTarget()

	def updateTarget(self):
		currentFolder = self["filelist"].getPath()
		if currentFolder is not None:
			self["target"].setText(currentFolder)
		else:
			self["target"].setText(_("Invalid Location"))


class JukeboxSummary(JukeboxDisplaySummary):
	def __init__(self, session, parent):
		JukeboxDisplaySummary.__init__(self, session, parent, CoverPixmap())


class JukeboxSummaryText(JukeboxDisplayListSummary):
	pass


class JukeboxText(ConfigText):
	def onSelect(self, session):
		# OK opens the full keyboard; omit the numerical-input help overlay.
		ConfigText.onSelect(self, None)


class JukeboxTagEditor(Setup):
	def __init__(self, session, song, fileTags):
		self.song = song
		self.fileTags = fileTags
		self.saving = False
		self.fileSaved = False
		self.original = musicDatabase.readSong(song.songId)
		if fileTags:
			metadata = AudioMetadata(song.fileName)
			self.original = {x: getattr(metadata, x) for x in (*MP3TagWriter.FRAMES, "comment")}
		self.fieldSettings = {x: JukeboxText(default=value, fixed_size=False) for x, value in self.original.items()}
		Setup.__init__(self, session, None, plugin="Extensions/Jukebox", PluginLanguageDomain=PluginLanguageDomain)
		self.skin = playerAppearance.load("JukeboxTagEditor")
		self.skinName = ["JukeboxTagEditor"]
		self.setTitle(_("Edit MP3 tags") if fileTags else _("Edit database entry"))
		self["editorTitle"] = StaticText(self.getTitle())
		self["fileName"] = StaticText(basename(song.fileName))

	def createSetup(self, appendItems=None, prependItems=None):
		labels = {"title": _("Title"), "artist": _("Artist"), "album": _("Album"), "albumArtist": _("Album artist"), "genre": _("Genre"), "date": _("Year / Date"), "track": _("Track number"), "disc": _("Disc number"), "composer": _("Composer"), "comment": _("Comment"), "copyright": _("Copyright")}
		description = _("Save writes changed MP3 tags and updates this database entry. Other tags and cover art are preserved.") if self.fileTags else _("Changes affect only the database. A new scan reads the tags from the music file again.")
		self.list = [(labels[x], self.fieldSettings[x], description) for x in labels if x in self.fieldSettings]
		self["config"].setList(self.list)

	def keySave(self):
		if self.saving:
			return
		if libraryScanner.isRunning:
			self.session.open(MessageBox, _("Wait for the library scan to finish."), type=MessageBox.TYPE_INFO)
			return
		values = {x: setting.value for x, setting in self.fieldSettings.items()}
		changes = {x: values[x] for x in values if values[x] != self.original[x]}
		for field in ("track", "disc"):
			if changes.get(field) and not fullmatch(r"[0-9]+(?:/[0-9]+)?", values[field]):
				self.session.open(MessageBox, _("Use a number or number/total for track and disc."), type=MessageBox.TYPE_ERROR)
				return
		if changes.get("date") and not fullmatch(r"[0-9]{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?", values["date"]):
			self.session.open(MessageBox, _("Use YYYY, YYYY-MM or YYYY-MM-DD for the date."), type=MessageBox.TYPE_ERROR)
			return
		if not changes:
			self.close()
			return
		self.saving = True
		self["footnote"].setText(_("Saving…"))
		self["footnote"].show()
		deferToThread(self.saveChanges, changes, values).addCallbacks(self.saved, self.saveFailed)

	def saveChanges(self, changes, values):
		if self.fileTags:
			MP3TagWriter.write(self.song.fileName, changes)
			self.fileSaved = True
			metadata = AudioMetadata(self.song.fileName)
			values = {x: getattr(metadata, x) for x in ("title", "artist", "album", "genre", "track", "date")}
		musicDatabase.updateSong(self.song.songId, values)
		return dict(values, songId=self.song.songId)

	def saved(self, values):
		self.saving = False
		self.close(values)

	def saveFailed(self, failure):
		self.saving = False
		self["footnote"].hide()
		message = _("MP3 tags were saved, but the database could not be updated: %s") if self.fileSaved else _("Could not save changes: %s")
		self.session.open(MessageBox, message % failure.getErrorMessage(), type=MessageBox.TYPE_ERROR)

	def keyCancel(self):
		if not self.saving:
			Setup.keyCancel(self)


class JukeboxSetup(Setup):
	def __init__(self, session, databasePath=True):
		Setup.__init__(self, session, "Jukebox", plugin="Extensions/Jukebox", PluginLanguageDomain=PluginLanguageDomain)
		self.skin = playerAppearance.load("JukeboxSetup")
		self.skinName = ["JukeboxSetup"]
		self["key_yellow"] = StaticText(_("Database"))
		self["libraryActions"] = HelpableActionMap(self, ["ColorActions", "JukeboxLibraryActions"], {
			"yellow": (self.keyLibrary, _("Save settings and open the database")),
			"openLibrary": (self.keyLibrary, _("Save settings and open the database")),
		}, -2)

	def keyLibrary(self):
		self.saveAll()
		self.close("library")

	def keySelect(self):
		current = self["config"].getCurrent()
		if current and isinstance(current[1], ConfigDirectory):
			self.session.openWithCallback(boundFunction(self.pathSelected, current[1]), JukeboxPath, current[1].value)
		else:
			Setup.keySelect(self)

	def pathSelected(self, setting, path):
		if path:
			setting.value = path
			self["config"].invalidateCurrent()

	def keySave(self):
		self.saveAll()
		self.close(True)

	def keyClose(self):
		self.keyCancel()


class JukeboxFileList(Screen):
	def __init__(self, session, serviceList):
		self.session = session
		Screen.__init__(self, session)
		self.skin = playerAppearance.load("JukeboxFileList")
		self["list"] = MusicFileList(
			config.plugins.jukebox.musicFolder.value if exists(config.plugins.jukebox.musicFolder.value) else "/media/",
			showDirectories=True,
			showFiles=True,
			matchingPattern=r"(?i)^.*\.(mp3|mp2|m4a|flac|ogg|oga|opus|wav|aiff|aif|aac|wma|ape|wv|m3u8?|pls|cue|e2pls)$",
			useServiceRef=False,
		)
		self["actions"] = ActionMap(
			["WizardActions", "DirectionActions", "ColorActions", "MenuActions"],
			{
				"ok": self.ok,
				"back": self.close,
				"menu": self.keyMenu,
				"info": self.keyInfo,
				"green": self.keyGreen,
				"up": self.moveUp,
				"down": self.moveDown,
				"right": self.moveRight,
				"left": self.moveLeft,
				"blue": self.appendFileToSongList,
				"yellow": self.insertFileToSongList,
				"red": self.close,
			},
			-1,
		)
		self.serviceList = serviceList
		self["headertext"] = Label()
		self["key_red"] = StaticText(_("Close"))
		self["key_green"] = StaticText(_("Play folder"))
		self["key_yellow"] = StaticText(_("Play next"))
		self["key_blue"] = StaticText(_("Append"))
		self["playlistHint"] = StaticText(_("TEXT: Playlist editor"))
		self["playlistActions"] = ActionMap(["JukeboxPlaylistActions"], {"openPlaylists": self.showPlaylists}, -2)
		self.player = None
		self.libraryRequested = False
		self.returnToPlayer = False
		self.libraryTimer = eTimer()
		self.libraryTimer.callback.append(self.openLibrary)
		self["libraryActions"] = ActionMap(["JukeboxLibraryActions"], {"openLibrary": self.openLibrary}, -2)
		self.onClose.append(self.onCloseCallback)
		self.onLayoutFinish.append(self.startRun)
		self.onShown.append(self.updateTarget)
		self.currentService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		self.session.nav.stopService()

		self.startPlayerScreenTimer = eTimer()
		self.startPlayerScreenTimer.timeout.get().append(self.keyInfo)

	def startRun(self):
		if config.plugins.jukebox.resumePlaylist.value:
			self.startPlayerTimer = eTimer()
			self.startPlayerTimer.callback.append(self.startPlayerTimerCallback)
			self.startPlayerTimer.start(0, 1)

	def startPlayerTimerCallback(self):
		with musicDatabase.connection() as connection:
			if connection is not None:
				cursor = connection.cursor()
				libraryMode = False
				songList = []
				cursor.execute("select song_id, filename, title, artist, album, genre, bitrate, length,  track, date, PTS from CurrentSongList;")
				for row in cursor:
					songList.append(
						(
							MusicItem(
								songId=row[0],
								text=basename(row[1]),
								fileName=row[1],
								title=row[2],
								artist=row[3],
								album=row[4],
								genre=row[5],
								bitrate=row[6],
								length=row[7],
								track=row[8],
								date=row[9],
								cuePts=row[10],
								formatMetadata=False,
							),
						)
					)
					if row[0] != 0:
						libraryMode = True
				cursor.close()
				if self.player is not None:
					self.player.doClose()
					self.player = None
				self.startPlayerScreenTimer.stop()
				count = len(songList)
				if count:
					# just to be sure, check the index , it's critical
					index = config.plugins.jukebox.lastSongIndex.value
					if index < 0 or index >= count:
						index = 0
					self.player = self.session.instantiateDialog(JukeboxPlayer, songList, index, libraryMode, self.currentService, self.serviceList, self)
					self.session.execDialog(self.player)

	def readCue(self, fileName):
		try:
			return [(MusicItem(**x),) for x in PlaylistReader.readCue(fileName)]
		except (OSError, ValueError) as error:
			self.session.open(MessageBox, _("Cannot read playlist: %s") % error, type=MessageBox.TYPE_ERROR)
			return []

	def readM3u(self, fileName):
		try:
			return [(MusicItem(**x),) for x in PlaylistReader.readM3u(fileName)]
		except (OSError, ValueError) as error:
			self.session.open(MessageBox, _("Cannot read playlist: %s") % error, type=MessageBox.TYPE_ERROR)
			return []

	def readPls(self, fileName):
		try:
			return [(MusicItem(**x),) for x in PlaylistReader.readPls(fileName)]
		except (OSError, ValueError) as error:
			self.session.open(MessageBox, _("Cannot read playlist: %s") % error, type=MessageBox.TYPE_ERROR)
			return []

	def keyGreen(self):
		songList = []
		count = 0
		for root, subFolders, files in walk(self["list"].getCurrentDirectory() or "/media"):
			files.sort()
			for fileName in files:
				if fileName and fileName.lower().endswith(AudioMetadata.EXTENSIONS):
					songList.append((MusicItem(text=fileName, fileName=join(root, fileName)),))
		if self.player is not None:
			self.player.doClose()
			self.player = None
		self.startPlayerScreenTimer.stop()
		count = len(songList)
		if count:
			self.player = self.session.instantiateDialog(JukeboxPlayer, songList, 0, False, self.currentService, self.serviceList, self)
			self.session.execDialog(self.player)
		else:
			self.session.open(MessageBox, _("No music files found!"), type=MessageBox.TYPE_INFO, timeout=20)

	def ok(self):
		if self["list"].canDescent():
			self["list"].descent()
			self.updateTarget()
		else:
			songList = []
			foundIndex = 0
			count = 0
			index = 0
			currentFileName = self["list"].getPath()
			if not currentFileName:
				return
			if currentFileName.lower().endswith(PlaylistFile.EXTENSIONS):
				try:
					songList = [(MusicItem(**x),) for x in PlaylistFile.read(currentFileName)]
				except (OSError, ValueError) as error:
					self.session.open(MessageBox, _("Cannot read playlist: %s") % error, type=MessageBox.TYPE_ERROR)
					return
				if not songList:
					self.session.open(MessageBox, _("No playable entries found"), type=MessageBox.TYPE_INFO)
					return
			else:
				files = listdir(self["list"].getCurrentDirectory())
				files.sort()
				for fileName in files:
					if fileName and fileName.lower().endswith(AudioMetadata.EXTENSIONS):
						songList.append((MusicItem(text=fileName, fileName=join(self["list"].getCurrentDirectory(), fileName)),))
						if self["list"].getPath() == join(self["list"].getCurrentDirectory(), fileName):
							foundIndex = index
						index += 1
			if self.player is not None:
				self.player.doClose()
				self.player = None
			self.startPlayerScreenTimer.stop()
			count = len(songList)
			if count:
				self.player = self.session.instantiateDialog(JukeboxPlayer, songList, foundIndex, False, self.currentService, self.serviceList, self)
				self.session.execDialog(self.player)
			else:
				self.session.open(MessageBox, _("No music files found!"), type=MessageBox.TYPE_INFO, timeout=20)

	def showPlaylists(self):
		self.startPlayerScreenTimer.stop()
		player = self.player
		self.session.openWithCallback(self.playlistsClosed, JukeboxSongList, player.songList if player else [], player.currentIndex if player else 0, player.libraryMode if player else False)

	def playlistsClosed(self, selection):
		if selection is None or selection == -1:
			return
		if self.player and self.player.songList:
			self.player.showPlaylistCallback(selection)
			self.session.execDialog(self.player)
		elif isinstance(selection, tuple) and len(selection) == 2:
			songList, index = selection
			if songList and isinstance(index, int) and 0 <= index < len(songList):
				self.player = self.session.instantiateDialog(JukeboxPlayer, songList, index, False, self.currentService, self.serviceList, self)
				self.session.execDialog(self.player)

	def openLibrary(self):
		self.startPlayerScreenTimer.stop()
		self.session.openWithCallback(self.libraryClosed, JukeboxLibrary, self.serviceList, self)

	def libraryClosed(self):
		if self.returnToPlayer:
			self.returnToPlayer = False
			self.keyInfo()

	def openSetup(self):
		self.startPlayerScreenTimer.stop()
		self.session.openWithCallback(self.setupFinished, JukeboxSetup, True)

	def setupFinished(self, result=None):
		if result == "library":
			self.openLibrary()

	def keyMenu(self):
		self.openSetup()

	def stopPlayingAndAppendFileToSongList(self):
		self.startPlayerScreenTimer.stop()
		if self.player is not None:
			self.player.doClose()
			self.player = None
		self.appendFileToSongList()
		self.startPlayerScreenTimer.start(JukeboxPlayer.RETURN_DELAY, True)

	def appendFileToSongList(self):
		playerAvailable = self.player is not None and self.player.songList
		fileName = self["list"].getFilename()
		if fileName and fileName.lower().endswith(AudioMetadata.EXTENSIONS):
			songList = []
			song = MusicItem(text=fileName, fileName=join(self["list"].getCurrentDirectory(), fileName))
			if playerAvailable:
				self.player.songList.append((song,))
				self.player.originalSongList.append((song,))
			else:
				songList.append((song,))
			if not playerAvailable:
				if self.player is not None:
					self.player.doClose()
					self.player = None
				self.player = self.session.instantiateDialog(JukeboxPlayer, songList, 0, False, self.currentService, self.serviceList, self)
				self.player.playSong(self.player.songList[self.player.currentIndex][0].fileName)
				self.player["coverArt"].onShow()
				self.player.playbackStarted = True
			else:
				self.player["nextTitle"].setText(self.player.getNextTitle())
				self.session.open(MessageBox, _("%s\nappended to songlist") % song.text, type=MessageBox.TYPE_INFO, timeout=3)

	def insertFileToSongList(self):
		if self.player is not None and self.player.songList:
			index = self.player.currentIndex
			fileName = self["list"].getFilename()
			if fileName and fileName.lower().endswith(AudioMetadata.EXTENSIONS):
				song = MusicItem(text=fileName, fileName=join(self["list"].getCurrentDirectory(), fileName))
				self.player.songList.insert(index + 1, (song,))
				self.player.originalSongList.insert(index + 1, (song,))
				self.player["nextTitle"].setText(self.player.getNextTitle())
				self.session.open(MessageBox, _("%s\ninserted and will be played as next song") % song.text, type=MessageBox.TYPE_INFO, timeout=3)
		else:
			self.appendFileToSongList()

	def keyInfo(self):
		self.startPlayerScreenTimer.stop()
		if self.player is not None:
			if self.player.songList:
				self.session.execDialog(self.player)

	def moveRight(self):
		self["list"].pageDown()
		self.lcdUpdate()

	def moveLeft(self):
		self["list"].pageUp()
		self.lcdUpdate()

	def moveUp(self):
		self["list"].up()
		self.lcdUpdate()

	def moveDown(self):
		self["list"].down()
		self.lcdUpdate()

	def updateTarget(self):
		currentFolder = self["list"].getCurrentDirectory()
		if currentFolder is None:
			currentFolder = _("Invalid Location")
		self["headertext"].setText(_("Filelist: %s") % currentFolder)
		self.lcdUpdate()
		if self.libraryRequested:
			self.libraryRequested = False
			self.libraryTimer.start(0, True)

	def lcdUpdate(self):
		self.startPlayerScreenTimer.start(JukeboxPlayer.RETURN_DELAY, True)
		entries = self["list"].getFileList()
		if not entries:
			for line in (1, 3, 4):
				self.summaries.setText("", line)
			return
		index = self["list"].getSelectionIndex()
		for offset, line in ((0, 1), (-1, 3), (1, 4)):
			entry = entries[(index + offset) % len(entries)][0]
			text = entry[FILE_NAME] or ""
			self.summaries.setText(f"/{text}" if entry[FILE_IS_DIR] else text, line)

	def onCloseCallback(self):
		self.libraryTimer.stop()
		self.startPlayerScreenTimer.stop()
		if self.player is not None:
			self.player.closePlayer()
			self.player.doClose()
			self.player = None
		if self.serviceList is None:
			self.session.nav.playService(self.currentService, adjust=False)
		else:
			current = ServiceReference(self.serviceList.getCurrentSelection())
			self.session.nav.playService(current.ref, adjust=False)
		if config.plugins.jukebox.rememberFolder.value:
			try:
				config.plugins.jukebox.musicFolder.value = self["list"].getCurrentDirectory() or "/media/"
				config.plugins.jukebox.musicFolder.save()
			except Exception:
				pass

	def createSummary(self):
		return JukeboxSummaryText


def main(session, **kwargs):
	openFileList(session, **kwargs)


def openFileList(session, **kwargs):
	serviceList = InfoBar.instance and InfoBar.instance.servicelist
	session.open(JukeboxFileList, serviceList)


def playerMenu(menuid, **kwargs):
	if menuid == "mainmenu" and config.plugins.jukebox.playerInMainMenu.value:
		return [(_("Jukebox"), openFileList, "jukebox_player", 46)]
	return []


def Plugins(**kwargs):
	descriptors = [PluginDescriptor(name=_("Jukebox"), description=_("Music player and database"), where=[PluginDescriptor.WHERE_PLUGINMENU], icon="jukebox.png", fnc=main)]
	if config.plugins.jukebox.playerInExtensions.value:
		descriptors.append(PluginDescriptor(name=_("Jukebox"), description=_("Music player and database"), where=[PluginDescriptor.WHERE_EXTENSIONSMENU], fnc=main))
	descriptors.append(PluginDescriptor(name=_("Jukebox"), description=_("Music player and database"), where=[PluginDescriptor.WHERE_MENU], fnc=playerMenu))
	return descriptors
