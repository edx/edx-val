""" Tests for the GCP -> edX language code backfill in migration 0006 """

import importlib

from ddt import data, ddt, unpack
from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from edxval.models import TranscriptFormat, TranscriptProviderType, Video, VideoTranscript
from edxval.tests import constants

migration = importlib.import_module('edxval.migrations.0006_backfill_language_code_gcp_to_edx')


class _SchemaEditorStub:  # pylint: disable=too-few-public-methods
    """Stands in for the schema editor the migration only uses to reach the connection alias."""

    connection = connection


@ddt
class Migration0006Tests(TestCase):
    """
    The forward backfill relabels GCP codes to canonical edX codes for every provider.

    Note that these run on SQLite, where string comparison is case-sensitive. The exact-match
    logic in the migration exists for MySQL's case-insensitive collation, which SQLite cannot
    reproduce, so the mappings differing only in case are not truly exercised here.
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

    @staticmethod
    def _run_forward():
        migration.gcp_to_edx_language_codes(global_apps, _SchemaEditorStub())

    @staticmethod
    def _run_reverse():
        migration.edx_to_gcp_language_codes(global_apps, _SchemaEditorStub())

    @data(
        ('es', 'es-419'),  # ambiguous GCP collapse; es-419 per product decision
        ('de', 'de-de'),
        ('it', 'it-it'),
        ('ko', 'ko-kr'),
        ('tr', 'tr-tr'),
        ('fr-CA', 'fr-ca'),
        ('pt-BR', 'pt-br'),
        ('pt-PT', 'pt-pt'),
        ('zh-CN', 'zh-cn'),
        ('fa', 'fa'),  # already a valid edX code; deliberately unmapped
        ('en', 'en'),  # identical in both formats
        ('zh-tw', 'zh-tw'),  # third-party plan code, not ours to reinterpret
    )
    @unpack
    def test_language_code_rewrite(self, gcp_code, expected):
        transcript = self._make_transcript(gcp_code)
        self._run_forward()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, expected)

    @data('es-419', 'de-de', 'fr-ca', 'pt-br', 'pt-pt', 'zh-cn')
    def test_already_canonical_rows_are_left_alone(self, edx_code):
        """
        A row written after the LP-1118 cutover must survive the backfill unchanged, and must
        not be reported as a collision.

        The warning matters as much as the value here. Under a case-insensitive collation a
        query for the GCP spelling also returns the canonical row, and without the exact match
        in `_rows_holding_exactly` that row is mistaken for one blocked by its own twin --
        leaving the data correct but telling an operator to resolve a duplicate that does not
        exist. On SQLite the comparison is case-sensitive, so only MySQL exercises this.
        """
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
        The GCP spellings are not valid edX codes for anyone, so provider is not a filter.

        Each provider gets its own video because unique_together allows only one row per
        (video, language_code).
        """
        video = Video.objects.create(
            client_video_id=provider, edx_video_id=f'video-{provider}', duration=10.0, status='test',
        )
        transcript = self._make_transcript('pt-BR', video=video, provider=provider)
        self._run_forward()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'pt-br')

    def test_orphaned_transcript_is_renamed(self):
        """`video` is nullable, and a row with no video cannot collide, so it always renames."""
        orphan = VideoTranscript.objects.create(
            video=None,
            language_code='zh-CN',
            provider=TranscriptProviderType.EDX_AI_TRANSLATIONS,
            file_format=TranscriptFormat.SRT,
        )
        self._run_forward()
        orphan.refresh_from_db()
        self.assertEqual(orphan.language_code, 'zh-cn')

    def test_collision_drops_the_stale_row(self):
        """
        Renaming into a code the same video already holds would break unique_together, and the
        pair is the duplicate caption-picker entry this migration exists to remove. The row
        still spelled the GCP way is the stale one, so it goes.
        """
        legacy = self._make_transcript('es')
        canonical = self._make_transcript('es-419', provider=TranscriptProviderType.CUSTOM)

        with self.assertLogs(migration.__name__, level='WARNING') as logs:
            self._run_forward()

        self.assertFalse(VideoTranscript.objects.filter(pk=legacy.pk).exists())
        canonical.refresh_from_db()
        self.assertEqual(canonical.language_code, 'es-419')
        self.assertEqual(VideoTranscript.objects.count(), 1)
        self.assertIn('dropping stale', logs.output[0])

    def test_rows_without_a_collision_are_never_deleted(self):
        """Only a video holding both spellings loses a row; everything else is renamed in place."""
        codes = ['es', 'es-419', 'de', 'pt-BR', 'pt-br', 'fa']
        for index, code in enumerate(codes):
            video = Video.objects.create(
                client_video_id=code, edx_video_id=f'video-{index}', duration=10.0, status='test',
            )
            self._make_transcript(code, video=video)

        self._run_forward()

        self.assertEqual(VideoTranscript.objects.count(), len(codes))

    def test_reverse_cannot_restore_a_dropped_row(self):
        """The round trip is lossy for a collided video: it comes back with only the GCP code."""
        self._make_transcript('es')
        self._make_transcript('es-419', provider=TranscriptProviderType.CUSTOM)

        self._run_forward()
        self._run_reverse()

        self.assertEqual(
            list(VideoTranscript.objects.filter(video=self.video).values_list('language_code', flat=True)),
            ['es'],
        )

    def test_reverse_is_lossy_for_es(self):
        transcript = self._make_transcript('es')
        self._run_forward()
        self._run_reverse()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'es')

    def test_reverse_restores_unambiguous_codes(self):
        transcript = self._make_transcript('pt-BR')
        self._run_forward()
        self._run_reverse()
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'pt-BR')
