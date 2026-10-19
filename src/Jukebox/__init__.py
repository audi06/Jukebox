from gettext import bindtextdomain, dgettext, gettext

from Components.Language import language
from Tools.Directories import SCOPE_PLUGINS, resolveFilename

PluginLanguageDomain = "Jukebox"
PluginLanguagePath = "Extensions/Jukebox/locale"


def localeInit():
	bindtextdomain(PluginLanguageDomain, resolveFilename(SCOPE_PLUGINS, PluginLanguagePath))


def _(text):
	translated = dgettext(PluginLanguageDomain, text)
	return gettext(text) if translated == text else translated


localeInit()
language.addCallback(localeInit)

__version__ = "2.0.0"
