import unittest

from bs4 import BeautifulSoup

import onlyfans_models as scraper


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class ModelImageScraperTests(unittest.TestCase):
    def test_image_url_supports_lazy_srcset_and_babewiki_subdomains(self):
        soup = BeautifulSoup(
            '<img data-src="https://images.babewiki.com/mia-small.jpg" '
            'srcset="https://images.babewiki.com/mia-640.jpg 640w, '
            'https://images.babewiki.com/mia-1280.jpg 1280w">',
            "html.parser",
        )
        self.assertEqual(
            scraper._image_url(soup.img),
            "https://images.babewiki.com/mia-small.jpg",
        )

    def test_rejects_lookalike_external_image_hosts(self):
        self.assertEqual(scraper._absolute_image("https://babewiki.com.attacker.test/a.jpg"), "")
        self.assertEqual(scraper._absolute_image("http://images.babewiki.com/a.jpg"), "")

    def test_profile_collects_gallery_images_from_responsive_markup(self):
        html = '''
        <html><head><title>Mia Khalifa Wiki</title></head><body>
          <h1>Mia Khalifa</h1>
          <img alt="Mia Khalifa's photo 1" src="/images/placeholder.gif"
            data-srcset="https://images.babewiki.com/mia-1-small.jpg 480w, https://images.babewiki.com/mia-1-large.jpg 1200w">
          <picture><source srcset="https://images.babewiki.com/mia-2.jpg 900w">
            <img alt="Mia Khalifa's photo 2" src="/images/placeholder.gif"></picture>
        </body></html>
        '''
        scraper._cache.clear()
        result = scraper.scrape_profile(
            "mia-khalifa",
            lambda *args, **kwargs: _Response(html),
        )
        self.assertEqual(result["name"], "Mia Khalifa")
        self.assertEqual(
            result["images"],
            [
                "https://images.babewiki.com/mia-1-large.jpg",
                "https://images.babewiki.com/mia-2.jpg",
            ],
        )
        self.assertEqual(result["image_url"], result["images"][0])

    def test_profile_uses_og_image_when_no_img_source_is_usable(self):
        html = '''<html><head><title>Test Model Wiki</title>
          <meta property="og:image" content="https://babewiki.com/images/fallback.jpg">
        </head><body><h1>Test Model</h1></body></html>'''
        scraper._cache.clear()
        result = scraper.scrape_profile(
            "test-model-og-fallback",
            lambda *args, **kwargs: _Response(html),
        )
        self.assertEqual(result["image_url"], "https://babewiki.com/images/fallback.jpg")
        self.assertEqual(result["images"], ["https://babewiki.com/images/fallback.jpg"])


if __name__ == "__main__":
    unittest.main()
