from bisect import bisect_right
from hashlib import sha256
from json import load
from os import makedirs, replace, unlink
from os.path import getsize, isfile, join
from re import IGNORECASE, compile as reCompile, sub
from tempfile import NamedTemporaryFile
from unicodedata import normalize
from urllib.parse import urlencode
from twisted.internet.defer import Deferred

from enigma import eTimer

from Tools.Downloader import DownloadWithProgress


class LyricsRequest:
	USER_AGENT = "Jukebox/2.0 (https://github.com/oe-alliance-plugins/Jukebox)"
	MAX_RESPONSE_BYTES = 1024 * 1024

	def __init__(self, metadata, cacheDirectory):
		self.metadata = metadata
		self.cacheDirectory = cacheDirectory
		self.downloader = None
		self.temporaryName = ""
		self.finishing = False
		self.cached = False
		self.error = ""
		self.retryCount = 0
		self.retryTimer = eTimer()
		self.retryTimer.callback.append(self.start)
		self.result = Deferred(self.cancel)
		self.timer = eTimer()
		self.timer.callback.append(self.timedOut)
		cacheKey = sha256(f"{metadata.artist}\n{metadata.title}\n{metadata.album}\n{metadata.duration}".encode("utf-8")).hexdigest()
		self.fileName = join(cacheDirectory, f"{cacheKey}.json")

	@staticmethod
	def normalized(text):
		return sub(r"[^\w]", "", normalize("NFKC", text).casefold())

	def readResult(self, fileName):
		if getsize(fileName) > self.MAX_RESPONSE_BYTES:
			raise ValueError("Lyrics response is too large")
		with open(fileName, encoding="utf-8") as handle:
			result = load(handle)
		if not isinstance(result, dict):
			raise ValueError("Invalid lyrics response")
		for key, value in (("trackName", self.metadata.title), ("artistName", self.metadata.artist)):
			if not isinstance(result.get(key), str) or self.normalized(result[key]) != self.normalized(value):
				raise ValueError("Lyrics do not match this track")
		if self.metadata.duration and (not isinstance(result.get("duration"), (int, float)) or abs(result["duration"] - self.metadata.duration) > 2):
			raise ValueError("Lyrics duration does not match this track")
		if any(result.get(x) is not None and not isinstance(result[x], str) for x in ("plainLyrics", "syncedLyrics")):
			raise ValueError("Invalid lyrics text")
		return result

	def start(self):
		if isfile(self.fileName):
			try:
				result = self.readResult(self.fileName)
				self.cached = True
				self.result.callback(result)
				return self.result
			except (OSError, ValueError):
				pass
		if not self.metadata.artist or not self.metadata.title or self.metadata.artist == "n/a":
			self.result.callback({})
			return self.result
		try:
			makedirs(self.cacheDirectory, exist_ok=True)
			with NamedTemporaryFile(dir=self.cacheDirectory, suffix=".part", delete=False) as handle:
				self.temporaryName = handle.name
			parameters = {"artist_name": self.metadata.artist, "track_name": self.metadata.title}
			if self.metadata.album:
				parameters["album_name"] = self.metadata.album
			if 0 < self.metadata.duration <= 3600:
				parameters["duration"] = self.metadata.duration
			self.downloader = DownloadWithProgress(f"https://lrclib.net/api/get?{urlencode(parameters)}", self.temporaryName)
			self.downloader.setAgent(self.USER_AGENT)
			self.downloader.addEnd(self.downloadFinished)
			self.downloader.addError(self.failed)
			self.downloader.addProgress(self.progress)
			self.timer.start(20000, True)
			self.downloader.start()
		except (OSError, ValueError) as error:
			self.failed(error)
		return self.result

	def progress(self, received, total):
		if received > self.MAX_RESPONSE_BYTES or total > self.MAX_RESPONSE_BYTES:
			self.failed(ValueError("Lyrics response is too large"))

	def downloadFinished(self, *args):
		if self.result.called:
			return
		self.timer.stop()
		try:
			result = self.readResult(self.temporaryName)
			replace(self.temporaryName, self.fileName)
			self.temporaryName = ""
			self.result.callback(result)
		except (OSError, ValueError) as error:
			self.failed(error)

	def timedOut(self):
		self.failed(TimeoutError("Lyrics download timed out"))

	def cancel(self, deferred):
		self.failed("Cancelled")

	def failed(self, error=None):
		if self.result.called or self.finishing:
			return
		self.finishing = True
		self.error = str(error or "")
		self.timer.stop()
		self.retryTimer.stop()
		if self.downloader:
			self.downloader.stop()
		if self.temporaryName:
			try:
				unlink(self.temporaryName)
			except OSError:
				pass
			self.temporaryName = ""
		if "503" in self.error and self.retryCount == 0:
			self.retryCount += 1
			self.downloader = None
			self.error = ""
			self.finishing = False
			self.retryTimer.start(3000, True)
		else:
			self.result.callback({})


class TimedLyrics:
	TIMESTAMP = reCompile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
	OFFSET = reCompile(r"\[offset:([+-]?\d+)\]", IGNORECASE)
	WORD_TIMESTAMP = reCompile(r"<\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?>")

	def __init__(self, entries=()):
		lines = {}
		for milliseconds, text in sorted(entries, key=lambda x: x[0]):
			milliseconds = max(0, milliseconds)
			if milliseconds in lines and text:
				lines[milliseconds] = f"{lines[milliseconds]}\n{text}".strip()
			else:
				lines[milliseconds] = text
		self.entries = list(lines.items())
		self.times = [x[0] for x in self.entries]

	@classmethod
	def fromText(cls, text):
		entries = []
		offsets = cls.OFFSET.findall(text)
		offset = int(offsets[-1]) if offsets else 0
		for line in text.splitlines():
			stamps = list(cls.TIMESTAMP.finditer(line))
			if not stamps or line[:stamps[0].start()].strip("\ufeff "):
				continue
			words = cls.WORD_TIMESTAMP.sub("", line[stamps[-1].end():]).strip()
			for stamp in stamps:
				minutes, seconds, fraction = stamp.groups()
				milliseconds = (int(minutes) * 60 + int(seconds)) * 1000 + int((fraction or "0").ljust(3, "0")) - offset
				entries.append((milliseconds, words))
		return cls(entries)

	@classmethod
	def fromMetadata(cls, metadata):
		if hasattr(metadata.tags, "getall"):
			for frame in metadata.tags.getall("SYLT"):
				if frame.format != 2 or frame.type != 1:
					continue
				entries = []
				text = ""
				start = 0
				for fragment, milliseconds in frame.text:
					for index, part in enumerate(fragment.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
						if index and text:
							entries.append((start, text.strip()))
							text = ""
						if not text:
							start = milliseconds
						text += part
				if text:
					entries.append((start, text.strip()))
				if entries:
					return cls(entries)
		for text in (metadata.lyrics, metadata.readLyricsFile(".lrc")):
			lyrics = cls.fromText(text)
			if lyrics.entries:
				return lyrics
		return cls()

	def indexAt(self, milliseconds):
		return bisect_right(self.times, milliseconds) - 1

	def lineAt(self, index):
		return self.entries[index][1] if 0 <= index < len(self.entries) else ""
