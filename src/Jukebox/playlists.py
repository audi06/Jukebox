from configparser import ConfigParser, Error as ConfigError
from os.path import dirname, isabs, join, normpath
from re import match
from urllib.parse import urlsplit


class PlaylistReader:
	@staticmethod
	def readText(fileName):
		with open(fileName, "rb") as handle:
			data = handle.read()
		try:
			return data.decode("utf-8-sig")
		except UnicodeDecodeError:
			return data.decode("cp1252", errors="replace")

	@staticmethod
	def resolvePath(fileName, entry):
		if urlsplit(entry).scheme.lower() in ("http", "https", "udp", "rtsp", "rtp", "mmp", "rtmp") or isabs(entry):
			return entry
		return normpath(join(dirname(fileName), entry.replace("\\", "/")))

	@classmethod
	def readM3u(cls, fileName):
		entries = []
		title = ""
		for line in cls.readText(fileName).splitlines():
			entry = line.strip()
			if entry.startswith("#EXTINF:"):
				title = entry.partition(",")[2]
			elif entry and not entry.startswith("#"):
				entries.append({"text": title or entry, "fileName": cls.resolvePath(fileName, entry)})
				title = ""
		return entries

	@classmethod
	def readPls(cls, fileName):
		text = cls.readText(fileName)
		if not text.lstrip().lower().startswith("[playlist]"):
			return cls.readM3u(fileName)
		parser = ConfigParser(interpolation=None, strict=False)
		try:
			parser.read_string(text)
			section = parser[next(x for x in parser.sections() if x.lower() == "playlist")]
			indexes = sorted(int(x[4:]) for x in section if x.startswith("file") and x[4:].isdigit())
			return [{"text": section.get(f"title{x}", section[f"file{x}"]), "fileName": cls.resolvePath(fileName, section[f"file{x}"])} for x in indexes]
		except (ConfigError, StopIteration):
			return []

	@classmethod
	def readCue(cls, fileName):
		entries = []
		album = albumArtist = title = artist = songFileName = ""
		track = None
		for line in cls.readText(fileName).splitlines():
			entry = line.strip()
			fileMatch = match(r'FILE\s+(?:"([^"]+)"|(\S+))\s+', entry, flags=2)
			textMatch = match(r'(TITLE|PERFORMER)\s+"(.*)"', entry, flags=2)
			trackMatch = match(r"TRACK\s+(\d+)\s+AUDIO", entry, flags=2)
			indexMatch = match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", entry, flags=2)
			if fileMatch:
				songFileName = cls.resolvePath(fileName, fileMatch[1] or fileMatch[2])
			elif trackMatch:
				track = int(trackMatch[1])
				title, artist = "", albumArtist
			elif textMatch:
				if textMatch[1].upper() == "TITLE":
					if track is None:
						album = textMatch[2]
					else:
						title = textMatch[2]
				elif track is None:
					albumArtist = textMatch[2]
				else:
					artist = textMatch[2]
			elif indexMatch and track is not None and songFileName:
				minutes, seconds, frames = (int(x) for x in indexMatch.groups())
				pts = (minutes * 60 + seconds) * 90000 + frames * 1200
				entries.append(
					{
						"text": title or f"Track {track}",
						"title": title or f"Track {track}",
						"artist": artist,
						"album": album,
						"fileName": songFileName,
						"cuePts": pts,
						"formatMetadata": False,
					}
				)
		return entries
