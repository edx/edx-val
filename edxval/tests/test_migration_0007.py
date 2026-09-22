""" Tests for the legacy -> edX language code backfill in migration 0007 """

import importlib
from unittest import skipUnless

from ddt import data, ddt, unpack
from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from edxval.models import TranscriptFormat, TranscriptProviderType, Video, VideoTranscript
from edxval.tests import constants

migration = importlib.import_module('edxval.migrations.0007_backfill_language_code_legacy_to_edx')


class _SchemaEditorStub:  # pylint: disable=too-few-public-methods
    """Stands in for the schema editor the migration only uses to reach the connection alias."""

    connection = connection


@ddt
class Migration0007Tests(TestCase):
    """
    The forward backfill folds legacy spellings into canonical edX codes for every provider.

    Unlike 0006, the legacy spellings are matched case-insensitively, which SQLite and MySQL
    agree on, so these tests exercise the same behaviour production will get.
    """

    def setUp(self):
        super().setUp()
        self.video = Video.objects.create(**constants.VIDEO_DICT_NEW_LINE)

    def _make_transcript(self, language_code, video=None, provider=TranscriptProviderType.EDX_AI_TRANSLATIONS):
        return VideoTranscript.objects.create(
            video=self.video if video is None else video,
            language_code=language_code,
            provider=provider,
            file_format=TranscriptFormat.SRT,
        )

    def _make_video(self, suffix):
        return Video.objects.create(
            client_video_id=str(suffix), edx_video_id=f'video-{suffix}', duration=10.0, status='test',
        )

    @staticmethod
    def _run_forward():
        migration.legacy_to_edx_language_codes(global_apps, _SchemaEditorStub())

    def _codes_on(self, video):
        return sorted(VideoTranscript.objects.filter(video=video).values_list('language_code', flat=True))

    @data(
        ('en-US', 'en'),
        ('en-GB', 'en'),
        ('iw', 'he'),
        ('zh-Hans-CN', 'zh-cn'),
        ('zh_HANS', 'zh-cn'),
        ('zh-Hans', 'zh-cn'),
        ('en-us', 'en'),
        ('IW', 'he'),
        ('zh-hans', 'zh-cn'),
        ('en', 'en'),
        ('he', 'he'),
        ('zh-cn', 'zh-cn'),
        ('uk', 'uk'),
        ('zh-tw', 'zh-tw'),
        ('es-419', 'es-419'),
    )
    @unpack
    def test_language_code_rewrite(self, legacy_code, expected):
        """
        Legacy spellings fold in any capitalisation; every other code survives untouched.

        The codes that stay put are the ones most at risk of being folded by mistake:
        "uk" is Ukrainian, "zh-tw" is a different language from "zh-cn", and "es-419" is
        what 0006 produced.
        """
        transcript = self._make_transcript(legacy_code)
        self._run_forward()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, expected)

    @data('en', 'he', 'zh-cn', 'uk', 'zh-tw')
    def test_already_canonical_rows_are_left_alone(self, edx_code):
        """A row that was never mis-spelled must survive untouched and raise no collision."""
        transcript = self._make_transcript(edx_code)

        with self.assertNoLogs(migration.__name__, level='WARNING'):
            self._run_forward()

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, edx_code)

    @data(
        TranscriptProviderType.CUSTOM,
        TranscriptProviderType.THREE_PLAY_MEDIA,
        TranscriptProviderType.CIELO24,
        TranscriptProviderType.EDX_AI_TRANSLATIONS,
    )
    def test_every_provider_is_relabelled(self, provider):
        """
        The legacy spellings are not valid edX codes for anyone, so provider is not a filter.

        Each provider gets its own video because unique_together allows only one row per
        (video, language_code).
        """
        video = self._make_video(provider)
        transcript = self._make_transcript('zh_HANS', video=video, provider=provider)
        self._run_forward()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'zh-cn')

    def test_orphaned_transcript_is_renamed(self):
        """`video` is nullable, and a row with no video cannot collide, so it always renames."""
        orphan = VideoTranscript.objects.create(
            video=None,
            language_code='en-US',
            provider=TranscriptProviderType.EDX_AI_TRANSLATIONS,
            file_format=TranscriptFormat.SRT,
        )
        self._run_forward()
        orphan.refresh_from_db()
        self.assertEqual(orphan.language_code, 'en')

    def test_collision_drops_the_stale_row(self):
        """The row still spelled the legacy way is the stale half of the duplicate, so it goes."""
        legacy = self._make_transcript('en-US')
        canonical = self._make_transcript('en', provider=TranscriptProviderType.CUSTOM)

        with self.assertLogs(migration.__name__, level='WARNING') as logs:
            self._run_forward()

        self.assertFalse(VideoTranscript.objects.filter(pk=legacy.pk).exists())
        canonical.refresh_from_db()
        self.assertEqual(canonical.language_code, 'en')
        self.assertEqual(VideoTranscript.objects.count(), 1)
        self.assertIn('dropping stale', logs.output[0])

    def test_two_legacy_spellings_on_one_video_collapse_to_one_row(self):
        """
        "en-US" and "en-GB" both fold into "en", and only one row can hold it. LEGACY_TO_EDX
        lists "en-US" first, so it is the one that survives.
        """
        british = self._make_transcript('en-GB')
        american = self._make_transcript('en-US', provider=TranscriptProviderType.CUSTOM)

        with self.assertLogs(migration.__name__, level='WARNING'):
            self._run_forward()

        self.assertFalse(VideoTranscript.objects.filter(pk=british.pk).exists())
        american.refresh_from_db()
        self.assertEqual(american.language_code, 'en')
        self.assertEqual(self._codes_on(self.video), ['en'])

    def test_the_whole_zh_hans_family_collapses_to_one_row(self):
        """Three spellings of Simplified Chinese on one video leave a single "zh-cn"."""
        survivor = self._make_transcript('zh-Hans-CN')
        self._make_transcript('zh_HANS')
        self._make_transcript('zh-Hans')

        self._run_forward()

        self.assertEqual(self._codes_on(self.video), ['zh-cn'])
        survivor.refresh_from_db()
        self.assertEqual(survivor.language_code, 'zh-cn')

    @skipUnless(connection.vendor == 'sqlite', 'MySQL cannot store both spellings on one video')
    def test_case_variants_on_one_video_collapse_to_one_row(self):
        """
        Under a case-sensitive collation one video can hold "zh-Hans" and "zh-hans", and both
        fold into "zh-cn". The oldest row wins; renaming both would break unique_together.
        """
        survivor = self._make_transcript('zh-Hans')
        duplicate = self._make_transcript('zh-hans')

        with self.assertLogs(migration.__name__, level='WARNING'):
            self._run_forward()

        self.assertFalse(VideoTranscript.objects.filter(pk=duplicate.pk).exists())
        survivor.refresh_from_db()
        self.assertEqual(survivor.language_code, 'zh-cn')

    def test_rows_without_a_collision_are_never_deleted(self):
        """Only a video holding two spellings of one code loses a row; the rest rename in place."""
        codes = ['en-US', 'en', 'en-GB', 'iw', 'he', 'zh-Hans', 'zh-cn', 'zh-tw', 'uk']
        for code in codes:
            self._make_transcript(code, video=self._make_video(code))

        self._run_forward()

        self.assertEqual(VideoTranscript.objects.count(), len(codes))

    def test_reverse_leaves_the_codes_canonical(self):
        """
        Unapplying must not guess: rewriting "en" back to "en-US" would hit every English
        transcript in the table, the overwhelming majority of which never held a legacy code.
        """
        transcript = self._make_transcript('en-US')

        self._run_forward()
        migration._irreversible(global_apps, _SchemaEditorStub())  # pylint: disable=protected-access

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'en')
