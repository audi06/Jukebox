from glob import glob
from os import makedirs
from os.path import basename, join, splitext
from shutil import which
from subprocess import run
from setuptools import Command
from setuptools.command.build import build as BuildBase


class BuildTranslations(Command):
	description = "Compile .po files into .mo files"
	user_options = []

	def initialize_options(self):
		pass  # Required setuptools command entry point; there are no options.

	def finalize_options(self):
		pass

	def run(self):
		localePath = join("Jukebox", "locale")
		domains = glob(join(localePath, "*.pot"))
		catalogs = glob(join(localePath, "*.po"))
		if domains and catalogs:
			compiler = which("msgfmt")
			if not compiler:
				raise RuntimeError("GNU gettext msgfmt is required to build translations")
			for catalog in catalogs:
				language = splitext(basename(catalog))[0]
				destination = join(localePath, language, "LC_MESSAGES")
				makedirs(destination, exist_ok=True)
				for domain in domains:
					output = join(destination, f"{splitext(basename(domain))[0]}.mo")
					print(f"Language compile {catalog} -> {output}")
					run((compiler, "--check", catalog, "-o", output), check=True)
		else:
			print("No translation domain or catalogs found")


class Build(BuildBase):
	# Compile first so build_py includes the generated catalogs on a clean checkout.
	sub_commands = [("build_trans", None)] + BuildBase.sub_commands


cmdclass = {
	"build": Build,
	"build_trans": BuildTranslations,
}
