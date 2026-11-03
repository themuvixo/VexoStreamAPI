import unittest

from bs4 import BeautifulSoup

import onlyfans_models as scraper
import thumbnail_utils as thumbs
from thumbnail_utils import clean_thumbnail_url, thumbnail_from_img


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


class VideoThumbnailTests(unittest.TestCase):
    def test_normalizer_picks_largest_srcset_candidate_and_decodes_entities(self):
        srcset = (
            "https://cdn.example.test/poster-320.jpg?x=1&amp;y=2 320w, "
            "https://cdn.example.test/poster-1280.jpg 1280w"
        )
        self.assertEqual(
            clean_thumbnail_url(srcset),
            "https://cdn.example.test/poster-1280.jpg",
        )

    def test_normalizer_resolves_relative_and_protocol_relative_urls(self):
        self.assertEqual(
            clean_thumbnail_url("../img/poster.jpg", "https://site.example/video/42"),
            "https://site.example/img/poster.jpg",
        )
        self.assertEqual(
            clean_thumbnail_url("//cdn.example.test/poster.webp"),
            "https://cdn.example.test/poster.webp",
        )

    def test_normalizer_rejects_placeholder_and_non_image_schemes(self):
        self.assertEqual(clean_thumbnail_url("data:image/gif;base64,abc"), "")
        self.assertEqual(clean_thumbnail_url("https://cdn.example.test/blank.gif"), "")

    def test_image_parser_prefers_lazy_poster_over_srcset_frames(self):
        soup = BeautifulSoup(
            '<picture><source srcset="/frame-small.jpg 320w, /frame-large.jpg 1280w">'
            '<img src="/blank.gif" data-src="/poster-lazy.jpg"></picture>',
            "html.parser",
        )
        self.assertEqual(
            thumbs.thumbnail_from_img(soup.img, "https://site.example/watch/42"),
            "https://site.example/poster-lazy.jpg",
        )

    def test_image_parser_prefers_uploader_thumb_attributes(self):
        soup = BeautifulSoup(
            '<img src="https://ei.phncdn.com/frame-7.jpg" data-src="https://ei.phncdn.com/frame-12.jpg" '
            'data-mediumthumb="https://ei.phncdn.com/poster.jpg">',
            "html.parser",
        )
        self.assertEqual(thumbs.thumbnail_from_img(soup.img), "https://ei.phncdn.com/poster.jpg")


class PosterResolverTests(unittest.TestCase):
    def test_only_provider_pages_are_fetched_and_results_are_cached(self):
        calls = []

        def fetch(url, provider):
            calls.append((url, provider))
            return {"thumbnail": "//img.example.test/" + provider + ".jpg"}

        cache = {}
        urls = [
            "https://www.xvideos.com/video123/title",
            "https://www.xvideos.com/video123/title",
            "http://127.0.0.1/admin",
            "https://evil.example.com/video",
            "https://www.pornhub.com/view_video.php?viewkey=abc",
        ]
        out = thumbs.resolve_posters(urls, fetch, cache)
        self.assertEqual(
            out,
            {
                "https://www.xvideos.com/video123/title": "https://img.example.test/xvideos.jpg",
                "https://www.pornhub.com/view_video.php?viewkey=abc": "https://img.example.test/pornhub.jpg",
            },
        )
        self.assertEqual(len(calls), 2)
        thumbs.resolve_posters(urls, fetch, cache)
        self.assertEqual(len(calls), 2, "second call must come from cache")

    def test_failed_lookup_returns_empty_poster_without_caching_the_miss(self):
        calls = []

        def fetch(url, provider):
            calls.append(url)
            raise RuntimeError("timeout")

        cache = {}
        url = "https://www.xnxx.com/video-1/x"
        out = thumbs.resolve_posters([url], fetch, cache)
        self.assertEqual(out, {url: ""})
        self.assertEqual(cache, {})
        thumbs.resolve_posters([url], fetch, cache)
        self.assertEqual(len(calls), 2, "a temporary miss must be retried")

    def test_batch_is_capped(self):
        urls = ["https://www.xnxx.com/video-%d/x" % i for i in range(60)]
        out = thumbs.resolve_posters(urls, lambda u, p: {"thumbnail": ""}, {})
        self.assertEqual(len(out), thumbs.MAX_POSTER_BATCH)


class FastPosterTests(unittest.TestCase):
    def test_og_image_wins_in_either_attribute_order(self):
        html = ('<head><meta name="twitter:image" content="https://cdn.example.test/tw.jpg">'
                '<meta content="https://ei.phncdn.com/poster.jpg?a=1&amp;b=2" property="og:image"></head>')
        self.assertEqual(thumbs.poster_from_html(html), "https://ei.phncdn.com/poster.jpg?a=1&b=2")

    def test_image_url_json_and_relative_values(self):
        self.assertEqual(
            thumbs.poster_from_html('<script>{"image_url":"https:\\/\\/img.example.test\\/p.jpg"}</script>'),
            "https://img.example.test/p.jpg",
        )
        self.assertEqual(
            thumbs.poster_from_html("<meta property='og:image' content='/i/p.jpg'>", "https://www.xvideos.com/video.x/y"),
            "https://www.xvideos.com/i/p.jpg",
        )

    def test_head_reader_stops_early_and_survives_a_timeout(self):
        sent = []

        def chunks():
            sent.append(1)
            yield b'<html><head><meta property="og:image" content="https://img.example.test/real.jpg">'
            sent.append(2)
            yield b"</head><body>" + b"x" * 50000
            sent.append(3)
            yield b"never read"

        poster, size = thumbs.read_page_head(chunks())
        self.assertEqual(poster, "https://img.example.test/real.jpg")
        self.assertEqual(sent, [1, 2], "reading must stop at </head>")

        def timing_out():
            yield b'<head><meta property="og:image" content="https://img.example.test/t.jpg">'
            raise TimeoutError("curl: (28) Operation timed out")

        self.assertEqual(thumbs.read_page_head(timing_out())[0], "https://img.example.test/t.jpg")


class PosterBrokerTests(unittest.TestCase):
    def setUp(self):
        self.jobs, self.now = [], [100.0]
        self.cache = {}

    def broker(self, fetch):
        return thumbs.PosterBroker(fetch, lambda fn, *a: self.jobs.append((fn, a)), self.cache,
                                   fail_ttl=60, clock=lambda: self.now[0])

    def run_jobs(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)

    def test_lookup_is_non_blocking_then_ready(self):
        url = "https://www.pornhub.com/view_video.php?viewkey=abc"
        b = self.broker(lambda u, p: "//ei.phncdn.com/real.jpg")
        self.assertEqual(b.lookup([url, url, "https://evil.example/x"]), ({url: ""}, [url]))
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(b.lookup([url]), ({url: ""}, [url]))
        self.assertEqual(len(self.jobs), 1, "an in-flight page is not fetched twice")
        self.run_jobs()
        self.assertEqual(b.lookup([url]), ({url: "https://ei.phncdn.com/real.jpg"}, []))

    def test_failure_is_reported_then_retried_after_ttl(self):
        url = "https://www.xvideos.com/video.k1/x"
        b = self.broker(lambda u, p: "")
        b.lookup([url])
        self.run_jobs()
        self.assertEqual(b.lookup([url]), ({url: ""}, []), "failed: not pending, site falls back")
        self.assertEqual(self.jobs, [])
        self.now[0] += 61
        self.assertEqual(b.lookup([url]), ({url: ""}, [url]))
        self.assertEqual(len(self.jobs), 1)


if __name__ == "__main__":
    unittest.main()
