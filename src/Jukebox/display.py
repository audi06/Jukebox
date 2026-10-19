from os.path import dirname, join
from xml.etree.ElementTree import parse, tostring

from enigma import eTimer, getDesktop

from Components.config import config
from Components.Label import Label
from Components.SystemInfo import BoxInfo
from Components.Sources.StaticText import StaticText
from Screens.Screen import Screen

from . import _


class JukeboxDisplaySkin:
	@staticmethod
	def load(width, height, browsing=False, textDisplay=False):
		if browsing:
			layout = "browser"
		elif textDisplay or height < 120 or width < 160:
			layout = "compact"
		else:
			layout = "landscape" if width >= height else "portrait"
		root = parse(join(dirname(__file__), "skin", "display.xml")).getroot()
		screen = next(x for x in root if x.get("name") == layout)
		return tostring(screen, encoding="unicode")


class JukeboxDisplaySummary(Screen):
	def __init__(self, session, parent, coverWidget):
		Screen.__init__(self, session, parent=parent)
		self.skinName = [f"{parent.__class__.__name__}Summary", "JukeboxSummary"]
		display = getDesktop(1)
		width, height = (display.size().width(), display.size().height()) if display else (132, 64)
		self.skin = JukeboxDisplaySkin.load(width, height, textDisplay=BoxInfo.getItem("displaytype") in ("textlcd", "7segment", "bwlcd255", "bwlcd140"))
		self["coverArt"] = coverWidget
		for name in ("Title", "Artist", "Album", "NextTitle", "PlaybackState", "PlaybackMode", "CoverPath"):
			self[name] = StaticText()
		self["Brand"] = StaticText(_("Jukebox"))
		for line in (1, 3, 4):
			self[f"text{line}"] = Label()
		self.coverPath = None
		self.updateTimer = eTimer()
		self.updateTimer.callback.append(self.refresh)
		self.onShow.append(self.startDisplay)
		self.onHide.append(self.updateTimer.stop)
		self.onClose.append(self.updateTimer.stop)

	def setText(self, text, line):
		if line in (1, 3, 4):
			self[f"text{line}"].setText(text)
			self[{1: "Title", 3: "Artist", 4: "NextTitle"}[line]].setText(text)

	def startDisplay(self):
		self.refresh()
		self.updateTimer.start(1000)

	def refresh(self):
		player = getattr(self.parent, "player", None) or self.parent
		if getattr(player, "closed", False):
			return
		metadata = getattr(player, "metadata", None)
		for source, widget in (("Title", "title"), ("Artist", "artist"), ("Album", "album"), ("NextTitle", "nextTitle"), ("PlaybackState", "playbackState"), ("PlaybackMode", "playbackMode")):
			if widget in player:
				text = player[widget].getText()
				if source == "Album" and metadata and metadata.date:
					text = text.removesuffix(f" {metadata.date}")
				self[source].setText(text)
			elif metadata and source in ("Title", "Artist", "Album"):
				self[source].setText(getattr(metadata, widget))
		for line, source in ((1, "Title"), (3, "Artist"), (4, "NextTitle")):
			self[f"text{line}"].setText(self[source].text)
		coverPath = ""
		if config.plugins.jukebox.showCover.value:
			if "coverArt" in player:
				coverPath = player["coverArt"].coverFileName
			elif metadata:
				if metadata.coverData:
					self["coverArt"].showEmbeddedCover(metadata.coverData)
					coverPath = self["coverArt"].coverFileName
				else:
					coverPath = metadata.coverFile()
		if coverPath != self.coverPath:
			self.coverPath = coverPath
			self["CoverPath"].setText(coverPath)
			if coverPath:
				self["coverArt"].showCoverFromFile(coverPath)
			else:
				self["coverArt"].showCoverFromFile(join(dirname(__file__), "skin", "cover-placeholder.png"))


class JukeboxDisplayListSummary(Screen):
	def __init__(self, session, parent):
		Screen.__init__(self, session, parent=parent)
		self.skinName = [f"{parent.__class__.__name__}Summary", "JukeboxSummaryText"]
		display = getDesktop(1)
		width, height = (display.size().width(), display.size().height()) if display else (132, 64)
		self.skin = JukeboxDisplaySkin.load(width, height, browsing=True)
		self["ScreenTitle"] = StaticText(parent.getTitle() or _("Jukebox"))
		for name in ("Title", "PreviousTitle", "NextTitle"):
			self[name] = StaticText()
		for line in (1, 3, 4):
			self[f"text{line}"] = Label()
		self.updateTimer = eTimer()
		self.updateTimer.callback.append(parent.lcdUpdate)
		# Session registers the summary after show(); populate the selection afterwards.
		self.onShow.append(self.startDisplay)
		self.onHide.append(self.updateTimer.stop)
		self.onClose.append(self.updateTimer.stop)

	def startDisplay(self):
		self["ScreenTitle"].setText(self.parent.getTitle() or _("Jukebox"))
		self.updateTimer.start(1, True)

	def setText(self, text, line):
		if line in (1, 3, 4):
			self[f"text{line}"].setText(text)
			self[{1: "Title", 3: "PreviousTitle", 4: "NextTitle"}[line]].setText(text)
