from os import fsync, replace, unlink
from os.path import dirname, realpath
from shutil import copy2
from tempfile import NamedTemporaryFile
from mutagen import MutagenError
from mutagen.apev2 import APEv2
from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TALB, TCOM, TCON, TCOP, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK


class MP3TagWriter:
	FRAMES = {"title": TIT2, "artist": TPE1, "album": TALB, "albumArtist": TPE2, "genre": TCON, "date": TDRC, "track": TRCK, "disc": TPOS, "composer": TCOM, "copyright": TCOP}
	APE_FIELDS = {"title": "Title", "artist": "Artist", "album": "Album", "albumArtist": "AlbumArtist", "genre": "Genre", "date": "Year", "track": "Track", "disc": "Disc", "composer": "Composer", "copyright": "Copyright", "comment": "Comment"}

	@staticmethod
	def write(fileName, changes):
		if not fileName.lower().endswith(".mp3"):
			raise ValueError("Only MP3 tags can be edited")
		if not changes:
			return
		if any(x not in MP3TagWriter.FRAMES and x != "comment" for x in changes):
			raise ValueError("Unsupported tag field")
		fileName = realpath(fileName)
		with NamedTemporaryFile(prefix=".jukebox-tags-", suffix=".mp3", dir=dirname(fileName), delete=False) as handle:
			temporaryName = handle.name
		try:
			copy2(fileName, temporaryName)
			try:
				tags = ID3(temporaryName)
				version = 3 if tags.version[1] == 3 else 4
			except ID3NoHeaderError:
				tags = ID3()
				version = 4
			for field, value in changes.items():
				if field == "comment":
					comments = tags.getall("COMM")
					comment = next((x for x in comments if not x.desc), comments[0] if comments else COMM(encoding=3, lang="eng", desc="", text=[]))
					tags.pop(comment.HashKey, None)
					if value:
						tags.add(COMM(encoding=3, lang=comment.lang, desc=comment.desc, text=[value]))
				else:
					frame = MP3TagWriter.FRAMES[field]
					tags.delall(frame.__name__)
					if value:
						values = [x.strip() for x in value.split(";") if x.strip()] if field in ("artist", "albumArtist", "genre", "composer") else [value]
						tags.add(frame(encoding=3, text=values))
			if version == 3:
				tags.update_to_v23()
			tags.save(temporaryName, v2_version=version, v23_sep=None)
			try:
				apeTags = APEv2(temporaryName)
			except MutagenError:
				apeTags = None
			if apeTags is not None:
				for field, value in changes.items():
					key = MP3TagWriter.APE_FIELDS[field]
					for alias in (("Date", "Year") if field == "date" else (key,)):
						apeTags.pop(alias, None)
					if value:
						apeTags[key] = "\0".join(x.strip() for x in value.split(";") if x.strip()) if field in ("artist", "albumArtist", "genre", "composer") else value
				apeTags.save(temporaryName)
			with open(temporaryName, "rb+") as handle:
				fsync(handle.fileno())
			replace(temporaryName, fileName)
		finally:
			try:
				unlink(temporaryName)
			except FileNotFoundError:
				pass
