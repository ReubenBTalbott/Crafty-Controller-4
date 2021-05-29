from app.classes.shared.helpers import helper
from app.classes.shared.console import console

import os
import json

import logging

logger = logging.getLogger(__name__)

class Translation():
    def __init__(self):
        self.translations_path = os.path.join(helper.root_dir, 'app', 'translations')
        self.cached_translation = None
        self.cached_translation_lang = None
    def translate(self, page, word):
        translated_word = None
        lang = helper.get_setting('language')
        fallback_lang = 'en_EN'

        lang_file_exists = helper.check_file_exists(
            os.path.join(
                self.translations_path, lang + '.json'
            )
        )

        translated_word = self.translate_inner(page, word, lang) \
            if lang_file_exists else self.translate_inner(page, word, fallback_lang)

        if translated_word:
            if isinstance(translated_word, dict): return json.dumps(translated_word)
            elif iter(translated_word) and not isinstance(translated_word, str): return '\n'.join(translated_word)
            return translated_word
        return 'Error while getting translation'
    def translate_inner(self, page, word, lang):
        lang_file = os.path.join(
            self.translations_path,
            lang + '.json'
        )
        try:
            if not self.cached_translation:
                with open(lang_file, 'r') as f:
                    data = json.load(f)
                    self.cached_translation = data
            elif self.cached_translation_lang != lang:
                with open(lang_file, 'r') as f:
                    data = json.load(f)
                    self.cached_translation = data
                    self.cached_translation_lang = lang
            else:
                data = self.cached_translation
            
            try:
                translated_page = data[page]
            except KeyError:
                logger.error('Translation File Error: page {} does not exist for lang {}'.format(page, lang))
                console.error('Translation File Error: page {} does not exist for lang {}'.format(page, lang))
                return None
            
            try:
                translated_word = translated_page[word]
                return translated_word
            except KeyError:
                logger.error('Translation File Error: word {} does not exist on page {} for lang {}'.format(word, page, lang))
                console.error('Translation File Error: word {} does not exist on page {} for lang {}'.format(word, page, lang))
                return None

        except Exception as e:
            logger.critical('Translation File Error: Unable to read {} due to {}'.format(lang_file, e))
            console.critical('Translation File Error: Unable to read {} due to {}'.format(lang_file, e))
            return None

translation = Translation()