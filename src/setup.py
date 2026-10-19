from setup_translate import cmdclass
from setuptools import setup


packageName = "Extensions.Jukebox"
setup(
	name="enigma2-plugin-extensions-jukebox",
	version="2.0.0",
	description="Jukebox music player and library for OpenATV 8",
	license="GPL-3.0-only",
	package_dir={packageName: "Jukebox"},
	packages=[packageName],
	package_data={packageName: ["*.png", "*.xml", "skin/*.xml", "skin/*.svg", "skin/*.png", "locale/*/LC_MESSAGES/*.mo", "LICENSE"]},
	cmdclass=cmdclass,
)
