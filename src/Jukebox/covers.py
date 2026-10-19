from hashlib import sha256
from json import load
from os import makedirs, replace, unlink
from os.path import getsize, isfile, join
from re import sub
from tempfile import NamedTemporaryFile
from time import monotonic
from unicodedata import normalize
from urllib.parse import urlencode
from twisted.internet.defer import Deferred

from enigma import eTimer

from Tools.Downloader import DownloadWithProgress


class CoverRequest:
	USER_AGENT = "Jukebox/2.0 (OpenATV Enigma2)"
	MAX_IMAGE_BYTES = 8 * 1024 * 1024
	lastQueryTime = 0

	def __init__(self, artist, album, cacheDirectory):
		self.artist = artist
		self.album = album
		self.cacheDirectory = cacheDirectory
		self.downloader = None
		self.temporaryName = ""
		self.result = Deferred(self.cancel)
		self.finishing = False
		self.startTimer = eTimer()
		self.startTimer.callback.append(self.startSearch)
		self.timer = eTimer()
		self.timer.callback.append(self.timedOut)
		cacheKey = sha256(f"{artist}\n{album}".encode("utf-8")).hexdigest()
		self.fileName = join(cacheDirectory, f"{cacheKey}.jpg")

	@staticmethod
	def normalized(text):
		return sub(r"[^\w]", "", normalize("NFKC", text).casefold())

	def start(self):
		if isfile(self.fileName):
			self.result.callback(self.fileName)
		elif self.artist and self.album and self.artist != "n/a" and self.album != "n/a":
			startTime = max(monotonic(), self.lastQueryTime + 1.1)
			CoverRequest.lastQueryTime = startTime
			self.startTimer.start(max(1, int((startTime - monotonic()) * 1000)), True)
		else:
			self.result.callback("")
		return self.result

	def startSearch(self):
		try:
			makedirs(self.cacheDirectory, exist_ok=True)
			artist = self.artist.replace('"', " ").replace("\\", " ")
			album = self.album.replace('"', " ").replace("\\", " ")
			query = urlencode({"query": f'artist:"{artist}" AND releasegroup:"{album}"', "fmt": "json", "limit": 5})
			self.fetch(f"https://musicbrainz.org/ws/2/release-group/?{query}", self.searchFinished)
		except OSError as error:
			self.failed(error)

	def fetch(self, url, callback):
		with NamedTemporaryFile(dir=self.cacheDirectory, suffix=".part", delete=False) as handle:
			self.temporaryName = handle.name
		self.downloader = DownloadWithProgress(url, self.temporaryName)
		self.downloader.setAgent(self.USER_AGENT)
		self.downloader.addEnd(lambda *args: callback())
		self.downloader.addError(self.failed)
		self.downloader.addProgress(self.progress)
		self.timer.start(20000, True)
		self.downloader.start()

	def progress(self, received, total):
		if received > self.MAX_IMAGE_BYTES or total > self.MAX_IMAGE_BYTES:
			self.failed(ValueError("Cover response is too large"))

	def searchFinished(self):
		self.timer.stop()
		if self.result.called:
			return
		try:
			with open(self.temporaryName, encoding="utf-8") as handle:
				response = load(handle)
			if not isinstance(response, dict) or not isinstance(response.get("release-groups", []), list):
				raise ValueError("Invalid MusicBrainz response")
			results = response.get("release-groups", [])
			self.cleanup()
			for result in results:
				if not isinstance(result, dict):
					continue
				artists = [x.get("name", "") for x in result.get("artist-credit", []) if isinstance(x, dict)]
				if self.normalized(self.album) == self.normalized(result.get("title", "")) and any(self.normalized(self.artist) == self.normalized(x) for x in artists):
					self.fetch(f"https://coverartarchive.org/release-group/{result['id']}/front-500", self.imageFinished)
					return
			self.result.callback("")
		except (OSError, ValueError, KeyError) as error:
			self.failed(error)

	def imageFinished(self):
		self.timer.stop()
		if self.result.called:
			return
		try:
			with open(self.temporaryName, "rb") as handle:
				header = handle.read(8)
			if getsize(self.temporaryName) > self.MAX_IMAGE_BYTES or not header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
				raise ValueError("Cover response is not a supported image")
			replace(self.temporaryName, self.fileName)
			self.temporaryName = ""
			self.result.callback(self.fileName)
		except (OSError, ValueError) as error:
			self.failed(error)

	def timedOut(self):
		self.failed(TimeoutError("Cover download timed out"))

	def cancel(self, deferred):
		self.timer.stop()
		self.startTimer.stop()
		if self.downloader:
			self.downloader.stop()
		self.cleanup()

	def cleanup(self):
		if self.temporaryName:
			try:
				unlink(self.temporaryName)
			except OSError:
				pass
			self.temporaryName = ""

	def failed(self, error=None):
		if self.result.called or self.finishing:
			return
		self.finishing = True
		self.cancel(self.result)
		if not self.result.called:
			print(f"[Jukebox] Cover download failed: {error}")
			self.result.callback("")
