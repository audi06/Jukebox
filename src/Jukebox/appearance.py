from os.path import dirname, join
from xml.etree.ElementTree import parse, tostring


class PlayerAppearance:
	COLORS = {"background": "#00132c29", "foreground": "#00f4e6c8", "selection": "#00813c2c", "selectedText": "#00fff4d8", "accent": "#00eacb80"}

	def load(self, name):
		root = parse(join(dirname(__file__), "skin", "skin.xml")).getroot()
		screen = next(x for x in root if x.get("name") == name)
		for element in screen.iter():
			for key, value in tuple(element.attrib.items()):
				if value.startswith("@asset:"):
					element.set(key, join(dirname(__file__), "skin", value.removeprefix("@asset:")))
				elif value.startswith("@"):
					element.set(key, self.COLORS[value[1:]])
		return tostring(screen, encoding="unicode")


playerAppearance = PlayerAppearance()
