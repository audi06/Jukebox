from json import dumps
from os import listdir
from os.path import basename, isdir, join
from re import compile as reCompile
from sys import argv
from xml.sax import make_parser
from xml.sax.handler import ContentHandler, LexicalHandler, property_lexical_handler


class TranslationExtractor(ContentHandler, LexicalHandler):
	ATTRIBUTES = ("text", "title", "value", "caption", "description", "red", "green", "yellow", "blue")
	EXCLUDED_FILES = {"dnsservers.xml"}
	HEX_COLOR = reCompile(r"#[0-9a-fA-F]+\Z")

	def __init__(self):
		super().__init__()
		self.entries = set()
		self.lastComment = None

	def comment(self, text):
		if "TRANSLATORS:" in text:
			self.lastComment = text

	def startElement(self, name, attributes):
		for attribute in self.ATTRIBUTES:
			value = attributes.get(attribute, "")
			if value.strip() and not self.HEX_COLOR.fullmatch(value):
				self.entries.add((value, self.lastComment))
				self.lastComment = None

	def extract(self, paths):
		parser = make_parser()
		parser.setContentHandler(self)
		parser.setProperty(property_lexical_handler, self)
		for path in paths:
			fileNames = sorted(join(path, x) for x in listdir(path) if x.endswith(".xml")) if isdir(path) else (path,)
			for fileName in fileNames:
				if basename(fileName) in self.EXCLUDED_FILES:
					continue
				self.entries.clear()
				self.lastComment = None
				parser.parse(fileName)
				for text, comment in sorted(self.entries, key=lambda x: (x[0], x[1] or "")):
					print(f"\n#: {fileName}")
					if comment:
						for line in comment.splitlines():
							print(f"#. {line}")
					print(f"msgid {dumps(text, ensure_ascii=False)}")
					print('msgstr ""')


translationExtractor = TranslationExtractor()
if __name__ == "__main__":
	translationExtractor.extract(argv[1:])
