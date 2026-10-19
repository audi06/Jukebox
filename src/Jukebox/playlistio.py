from os import fsync, replace, unlink
from os.path import basename, dirname, exists, isabs, join, splitext
from tempfile import NamedTemporaryFile

from enigma import eServiceReference

from ServiceReference import ServiceReference
from Components.Playlist import PlaylistIOInternal

from .playlists import PlaylistReader


class PlaylistFile:
	EXTENSIONS = (".e2pls", ".m3u", ".m3u8", ".pls", ".cue")
	WRITE_EXTENSIONS = (".e2pls", ".m3u")

	@classmethod
	def read(cls, fileName):
		extension = splitext(fileName)[1].lower()
		if extension == ".e2pls":
			services = PlaylistIOInternal().open(fileName)
			if services is None:
				raise OSError(fileName)
			entries = []
			for service in services:
				reference = service.ref
				path = reference.getPath()
				if reference.valid() and path:
					if not isabs(path) and "://" not in path:
						path = PlaylistReader.resolvePath(fileName, path)
						reference.setPath(path)
					entries.append({"fileName": path, "text": reference.getName() or basename(path), "title": reference.getName(), "serviceReference": reference.toString()})
		elif extension in (".m3u", ".m3u8"):
			entries = PlaylistReader.readM3u(fileName)
		elif extension == ".pls":
			entries = PlaylistReader.readPls(fileName)
		elif extension == ".cue":
			entries = PlaylistReader.readCue(fileName)
		else:
			raise ValueError("Unsupported playlist format")
		return entries

	@classmethod
	def destination(cls, directory, name):
		name = name.strip()
		if not name or name in (".", "..") or any(x in name for x in ("/", "\\", "\0", "\r", "\n")):
			raise ValueError("Invalid playlist name")
		if not splitext(name)[1]:
			name = f"{name}.e2pls"
		if splitext(name)[1].lower() not in cls.WRITE_EXTENSIONS:
			raise ValueError("Use .e2pls or .m3u")
		stem, extension = splitext(name)
		return join(directory, f"{stem}{extension.lower()}")

	@classmethod
	def write(cls, fileName, songs, overwrite=False):
		extension = splitext(fileName)[1].lower()
		if extension not in cls.WRITE_EXTENSIONS:
			raise ValueError("Use .e2pls or .m3u")
		if not songs:
			raise ValueError("The playlist is empty")
		if any(x.cuePts is not None for x in songs):
			raise ValueError("CUE track positions cannot be saved in MediaPlayer playlists")
		if exists(fileName) and not overwrite:
			raise FileExistsError(fileName)
		playlist = PlaylistIOInternal()
		lines = ["#EXTM3U"]
		for song in songs:
			if not song.fileName or any(x in song.fileName for x in ("\0", "\r", "\n")):
				raise ValueError("Invalid playlist entry")
			title = (song.title or song.text or basename(song.fileName)).replace("\r", " ").replace("\n", " ").replace("\0", "")
			reference = eServiceReference(song.serviceReference) if song.serviceReference else eServiceReference(4097, 0, song.fileName)
			reference.setName(title)
			playlist.addService(ServiceReference(reference))
			lines.extend((f"#EXTINF:-1,{title}", song.fileName))
		with NamedTemporaryFile(prefix=".jukebox-playlist-", dir=dirname(fileName), delete=False) as handle:
			temporaryName = handle.name
		try:
			if extension == ".e2pls":
				playlist.save(temporaryName)
			else:
				# No BOM or blank lines: the native MediaPlayer parser stops at blank lines.
				with open(temporaryName, "w", encoding="utf-8", newline="\n") as handle:
					handle.write("\n".join(lines) + "\n")
			with open(temporaryName, "rb+") as handle:
				fsync(handle.fileno())
			if exists(fileName) and not overwrite:
				raise FileExistsError(fileName)
			replace(temporaryName, fileName)
		finally:
			try:
				unlink(temporaryName)
			except FileNotFoundError:
				pass
