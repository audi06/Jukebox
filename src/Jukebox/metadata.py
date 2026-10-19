from base64 import b64decode
from datetime import timedelta
from os import listdir
from os.path import basename, dirname, isfile, join, splitext
from mutagen import File as MutagenFile, MutagenError
from mutagen.apev2 import APEBinaryValue, APETextValue, APEv2
from mutagen.flac import Picture
from mutagen.id3 import ID3, ID3NoHeaderError


class AudioMetadata:
	EXTENSIONS = (".mp3", ".mp2", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif", ".aac", ".wma", ".ape", ".wv")
	FIELDS = {
		"title": ("TIT2", "title", "\xa9nam"),
		"artist": ("TPE1", "artist", "\xa9ART", "Author"),
		"album": ("TALB", "album", "\xa9alb", "WM/AlbumTitle"),
		"albumArtist": ("TPE2", "albumartist", "aART", "WM/AlbumArtist"),
		"genre": ("TCON", "genre", "\xa9gen", "WM/Genre"),
		"date": ("TDRC", "TYER", "date", "year", "\xa9day", "WM/Year"),
		"track": ("TRCK", "tracknumber", "track", "trkn", "WM/TrackNumber"),
		"disc": ("TPOS", "discnumber", "disc", "disk", "WM/PartOfSet"),
		"composer": ("TCOM", "composer", "\xa9wrt", "WM/Composer"),
		"comment": ("COMM", "comment", "description", "\xa9cmt", "Description"),
		"copyright": ("TCOP", "copyright", "cprt", "Copyright"),
		"encoder": ("TENC", "encodedby", "\xa9too", "WM/EncodedBy"),
		"bpm": ("TBPM", "bpm", "tmpo", "WM/BeatsPerMinute"),
		"lyrics": ("USLT", "lyrics", "unsyncedlyrics", "\xa9lyr", "WM/Lyrics"),
	}

	def __init__(self, fileName):
		self.fileName = fileName
		self.audio = None
		self.tags = {}
		self.apeTags = {}
		self.error = ""
		self.duration = 0
		self.bitrate = 0
		self.sampleRate = 0
		self.channels = 0
		self.coverData = None
		self.coverMime = ""
		for field in self.FIELDS:
			setattr(self, field, "")
		self.title = splitext(basename(fileName))[0]
		if not isfile(fileName):
			return
		try:
			self.audio = MutagenFile(fileName, easy=False)
			if self.audio is not None:
				self.tags = self.audio.tags if self.audio.tags is not None else {}
				self.duration = max(0, int(getattr(self.audio.info, "length", 0)))
				self.bitrate = int(getattr(self.audio.info, "bitrate", 0) or 0) // 1000
				self.sampleRate = int(getattr(self.audio.info, "sample_rate", 0) or 0)
				self.channels = int(getattr(self.audio.info, "channels", 0) or 0)
		except (OSError, MutagenError, ValueError) as error:
			self.error = str(error)
		if not self.tags and fileName.lower().endswith((".mp3", ".mp2")):
			try:
				self.tags = ID3(fileName)
			except (OSError, ID3NoHeaderError, MutagenError):
				pass
		if fileName.lower().endswith((".mp3", ".mp2")) and not isinstance(self.tags, APEv2):
			try:
				self.apeTags = APEv2(fileName)
			except (OSError, MutagenError):
				pass
		for field, keys in self.FIELDS.items():
			value = self.readField(keys)
			if value:
				setattr(self, field, value)
		self.readCover()

	@staticmethod
	def text(value):
		if value is None:
			return ""
		if isinstance(value, bytes):
			try:
				return value.decode("utf-8")
			except UnicodeDecodeError:
				return value.decode("cp1252", errors="replace")
		if hasattr(value, "text"):
			return AudioMetadata.text(value.text)
		if isinstance(value, tuple) and all(isinstance(x, int) for x in value):
			return "/".join(str(x) for x in value if x)
		if isinstance(value, (list, tuple, APETextValue)):
			return "; ".join(AudioMetadata.text(x) for x in value)
		return str(value)

	def readField(self, keys):
		for tags in (self.tags, self.apeTags):
			for key in keys:
				try:
					values = tags.getall(key) if isinstance(tags, ID3) else tags.get(key)
				except (KeyError, ValueError):
					# Vorbis and APE reject MP4 atom names containing non-ASCII characters.
					continue
				if values:
					return self.text(values)
		return ""

	def readCover(self):
		pictures = []
		if isinstance(self.tags, ID3):
			pictures = self.tags.getall("APIC")
		elif self.audio is not None and hasattr(self.audio, "pictures"):
			pictures = self.audio.pictures
		elif self.tags.get("metadata_block_picture"):
			for value in self.tags["metadata_block_picture"]:
				try:
					pictures.append(Picture(b64decode(value)))
				except (ValueError, MutagenError):
					continue
		if pictures:
			picture = next((x for x in pictures if x.type == 3), pictures[0])
			# Linked APIC images are URLs rather than embedded image data.
			if picture.mime != "-->":
				self.coverData = picture.data
				self.coverMime = picture.mime
		elif self.tags.get("covr"):
			self.coverData = bytes(self.tags["covr"][0])
			self.coverMime = "image/png" if self.coverData.startswith(b"\x89PNG") else "image/jpeg"
		elif self.tags.get("coverart"):
			try:
				self.coverData = b64decode(self.tags["coverart"][0])
				self.coverMime = self.text(self.tags.get("coverartmime", ["image/jpeg"])[0])
			except ValueError:
				pass
		if not self.coverData:
			for tags in (self.tags, self.apeTags):
				cover = tags.get("Cover Art (Front)")
				if isinstance(cover, APEBinaryValue):
					self.coverData = bytes(cover).partition(b"\x00")[2]
					self.coverMime = "image/png" if self.coverData.startswith(b"\x89PNG") else "image/jpeg"
					break

	def coverFile(self):
		folder = dirname(self.fileName)
		coverNames = (
			f"{splitext(basename(self.fileName))[0]}.jpg",
			f"{splitext(basename(self.fileName))[0]}.png",
			"folder.jpg",
			"folder.png",
			"cover.jpg",
			"cover.png",
			"coverart.jpg",
			"front.jpg",
			"front.png",
			"folder.jpeg",
			"cover.jpeg",
		)
		try:
			files = {x.casefold(): x for x in listdir(folder)}
		except OSError:
			return ""
		return next((join(folder, files[x.casefold()]) for x in coverNames if x.casefold() in files and isfile(join(folder, files[x.casefold()]))), "")

	def lyricsText(self):
		if self.lyrics:
			return self.lyrics.replace("\r\n", "\n").replace("\r", "\n")
		if isinstance(self.tags, ID3):
			frames = self.tags.getall("SYLT")
			if frames:
				return "\n".join(x[0] for x in frames[0].text)
		return self.readLyricsFile(".lrc") or self.readLyricsFile(".txt")

	def readLyricsFile(self, extension):
		path = join(dirname(self.fileName), f"{splitext(basename(self.fileName))[0]}{extension}")
		try:
			with open(path, "rb") as handle:
				return self.text(handle.read()).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
		except OSError:
			return ""

	def allTags(self):
		rows = []
		tags = dict(self.tags)
		tags.update({f"APEv2 {x}": self.apeTags[x] for x in self.apeTags})
		for key in sorted(tags):
			value = tags[key]
			if key.startswith(("APIC", "GEOB", "PRIV")) or key.lower() in ("covr", "metadata_block_picture", "coverart"):
				data = getattr(value, "data", value)
				text = f"{len(data)} bytes"
			else:
				text = value.pprint() if hasattr(value, "pprint") else self.text(value)
			rows.append(f"{key}: {text}")
		return "\n\n".join(rows)

	@property
	def length(self):
		return str(timedelta(seconds=self.duration)) if self.duration else ""

	@property
	def trackNumber(self):
		try:
			return int(self.track.split("/", 1)[0])
		except ValueError:
			return -1
