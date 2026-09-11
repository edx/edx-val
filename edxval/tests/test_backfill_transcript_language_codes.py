"""
Tests for the backfill_transcript_language_codes management command.

The race tests reach into the command's internals on purpose: simulating a concurrent
writer means interposing at a specific point inside the run.
"""
# pylint: disable=protected-access
from io import StringIO
from unittest import mock

import ddt
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.db import models
from django.db.models import Q
from django.test import TestCase

from edxval.management.commands.backfill_transcript_language_codes import Command
from edxval.models import CourseVideo, TranscriptProviderType, Video, VideoTranscript

COURSE_ID = 'course-v1:edX+Val+2024'
OTHER_COURSE_ID = 'course-v1:edX+Val+2025'


def _widen_language_lookups(args, kwargs):
    """Rewrite `language_code` lookups into the wider set MySQL's collation would return."""
    if 'language_code' in kwargs:
        kwargs = dict(kwargs)
        kwargs['language_code__iexact'] = kwargs.pop('language_code')
    if 'language_code__in' in kwargs:
        kwargs = dict(kwargs)
        # No single ORM lookup spells "IN, case-insensitively"; OR the members together.
        # Q(pk__in=[]) is the identity for OR here, and matches nothing on its own.
        match_any = Q(pk__in=[])
        for value in kwargs.pop('language_code__in'):
            match_any |= Q(language_code__iexact=value)
        args = args + (match_any,)
    return args, kwargs


class _CaseInsensitiveQuerySet(models.QuerySet):
    """
    A queryset that matches `language_code` the way MySQL does.

    edx-platform runs this command on MySQL, whose default collation is case-insensitive:
    `filter(language_code='pt-BR')` also returns rows stored as 'pt-br'. The test settings use
    SQLite, which compares text case-sensitively, so there every such filter already returns
    exactly the rows the command's own Python-side comparison would keep -- which makes those
    comparisons no-ops and leaves the guards written for the collation untested.
    """

    def filter(self, *args, **kwargs):
        args, kwargs = _widen_language_lookups(args, kwargs)
        return super().filter(*args, **kwargs)

    def exclude(self, *args, **kwargs):
        args, kwargs = _widen_language_lookups(args, kwargs)
        return super().exclude(*args, **kwargs)


def mysql_collation_manager():
    """A drop-in `VideoTranscript.objects` that matches language codes as MySQL would."""
    manager = _CaseInsensitiveQuerySet.as_manager()
    manager.model = VideoTranscript
    return manager


def make_video(edx_video_id, course_id=COURSE_ID):
    """Create a Video, optionally linked to a course."""
    video = Video.objects.create(
        edx_video_id=edx_video_id, client_video_id=edx_video_id, duration=10.0, status='file_complete',
    )
    if course_id:
        CourseVideo.objects.create(video=video, course_id=course_id)
    return video


def make_transcript(video, language_code, provider=TranscriptProviderType.EDX_AI_TRANSLATIONS, body=b'subtitle'):
    """Create a VideoTranscript with a real stored file."""
    transcript = VideoTranscript.objects.create(
        video=video, language_code=language_code, provider=provider, file_format='srt',
    )
    transcript.transcript.save(f'{language_code}-{transcript.id}.srt', ContentFile(body))
    return transcript


def run(**kwargs):
    """Invoke the command and return its stdout."""
    out = StringIO()
    call_command('backfill_transcript_language_codes', stdout=out, **kwargs)
    return out.getvalue()


@ddt.ddt
class RenameTests(TestCase):
    """Straightforward renames, with no competing transcript."""

    def test_dry_run_changes_nothing(self):
        transcript = make_transcript(make_video('v1'), 'pt-BR')

        output = run()

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'pt-BR')
        self.assertIn('DRY-RUN', output)
        self.assertIn("'pt-BR' -> 'pt-br'", output)

    @ddt.unpack
    @ddt.data(
        ('de', 'de-de'),
        ('es', 'es-419'),
        ('fr-CA', 'fr-ca'),
        ('it', 'it-it'),
        ('ko', 'ko-kr'),
        ('pt-BR', 'pt-br'),
        ('pt-PT', 'pt-pt'),
        ('tr', 'tr-tr'),
        ('zh-CN', 'zh-cn'),
    )
    def test_renames_each_legacy_code(self, legacy_code, canonical_code):
        transcript = make_transcript(make_video(f'v-{legacy_code}'), legacy_code)

        run(commit=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, canonical_code)

    def test_rename_keeps_the_stored_file(self):
        """
        The rename must not go through create_or_update, whose save_transcript mints a fresh
        uuid filename when called without file data and strands the real object in storage.
        """
        transcript = make_transcript(make_video('v2'), 'zh-CN', body=b'important captions')
        original_name = transcript.transcript.name

        run(commit=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.transcript.name, original_name)
        self.assertTrue(transcript.transcript.storage.exists(original_name))
        with transcript.transcript.open() as handle:
            self.assertEqual(handle.read(), b'important captions')

    @ddt.data('fa', 'fa-ir', 'en', 'es-419', 'zh-tw', 'zh-cmn', 'zh-yue')
    def test_leaves_other_codes_alone(self, language_code):
        """Canonical codes, and third-party codes that never came from the pipeline."""
        transcript = make_transcript(make_video(f'v-{language_code}'), language_code)

        run(commit=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, language_code)

    def test_orphaned_transcript_is_renamed(self):
        """`video` is nullable, so a row with no video cannot collide with anything."""
        transcript = VideoTranscript.objects.create(
            video=None,
            language_code='ko',
            file_format='srt',
            provider=TranscriptProviderType.EDX_AI_TRANSLATIONS,
        )

        run(commit=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'ko-kr')

    def test_is_idempotent(self):
        make_transcript(make_video('v3'), 'it')

        run(commit=True)
        second_run = run()

        self.assertNotIn("'it' -> 'it-it'", second_run)
        self.assertEqual(VideoTranscript.objects.filter(language_code='it-it').count(), 1)

    def test_processes_every_row_across_batches(self):
        for index in range(5):
            make_transcript(make_video(f'batch-{index}'), 'de')

        run(commit=True, batch_size=2)

        self.assertEqual(VideoTranscript.objects.filter(language_code='de').count(), 0)
        self.assertEqual(VideoTranscript.objects.filter(language_code='de-de').count(), 5)


class CaseSensitivityTests(TestCase):
    """
    Four of the nine mappings differ only by case, and edx-platform runs this command on
    MySQL, whose default collation matches 'pt-BR' when asked for 'pt-br'. A row can therefore
    look like its own duplicate, and a row already migrated can look like a fresh candidate.

    These tests patch `VideoTranscript.objects` to match language codes the way MySQL does,
    because the SQLite database the test settings use does not. Without that patch the
    database-side filters return only exact matches by themselves, the command's Python-side
    comparisons never change an outcome, and deleting them leaves the suite green.

    The assertions here are deliberately about the run's reported totals as well as the final
    rows: several ways of losing the guards leave the data correct but quietly reclassify rows
    as candidates, collisions, or races.

    Two nearby guards are knowingly left unpinned, because neither changes an outcome there is
    anything to assert on: the chunk exclusion in `_rewrite` and the exact re-check in
    `_locked_incumbent` only narrow a hint that `_process_one` re-validates under a lock. Their
    comments say so.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(VideoTranscript, 'objects', mysql_collation_manager())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_collation_is_actually_simulated(self):
        """
        Guard the harness itself. If the patch stops widening lookups, every other test in
        this class silently reverts to exercising SQLite's case-sensitive behaviour and goes
        on passing for the wrong reason.
        """
        make_transcript(make_video('collation'), 'pt-BR')

        self.assertEqual(VideoTranscript.objects.filter(language_code='pt-br').count(), 1)
        self.assertEqual(VideoTranscript.objects.filter(language_code__in=['pt-br']).count(), 1)

    def test_case_only_rename_does_not_delete_the_row(self):
        transcript = make_transcript(make_video('v4'), 'pt-BR')
        file_name = transcript.transcript.name

        output = run(commit=True, resolve_duplicates=True)

        self.assertTrue(VideoTranscript.objects.filter(id=transcript.id).exists())
        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'pt-br')
        self.assertTrue(transcript.transcript.storage.exists(file_name))
        # The collation offers this row to itself as the incumbent holding 'pt-br'. It must
        # be renamed outright: not deleted as a duplicate, not skipped as a collision.
        self.assertIn('Renamed 1 transcript(s); removed 0 duplicate(s); skipped 0.', output)

    def test_already_canonical_rows_are_untouched(self):
        transcript = make_transcript(make_video('v5'), 'pt-br')
        file_name = transcript.transcript.name

        output = run(commit=True, resolve_duplicates=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'pt-br')
        self.assertTrue(transcript.transcript.storage.exists(file_name))
        # The scan for 'pt-BR' matches this row under the collation; only the exact
        # comparison in _exact_match_ids keeps it off the candidate list entirely.
        self.assertNotIn("'pt-BR' -> 'pt-br'", output)
        self.assertIn('Renamed 0 transcript(s); removed 0 duplicate(s); skipped 0.', output)

    def test_mixed_case_population_survives(self):
        legacy = make_transcript(make_video('v6'), 'zh-CN')
        canonical = make_transcript(make_video('v7'), 'zh-cn')

        output = run(commit=True, resolve_duplicates=True)

        legacy.refresh_from_db()
        canonical.refresh_from_db()
        self.assertEqual(legacy.language_code, 'zh-cn')
        self.assertEqual(canonical.language_code, 'zh-cn')
        self.assertEqual(VideoTranscript.objects.count(), 2)
        # The scan for 'zh-CN' also matches v7's already-canonical row. Counting it would
        # report two candidates and then discard one as "changed under us".
        self.assertIn("'zh-CN' -> 'zh-cn': 1 candidate(s)", output)
        self.assertNotIn('changed under us', output)

    def test_out_of_scope_tally_ignores_already_canonical_rows(self):
        """
        The out-of-scope diagnostic narrows with a database `IN` over the legacy codes, which
        this collation widens to the canonical spellings too. Only rows genuinely still
        holding a legacy code belong in the tally.
        """
        make_transcript(make_video('oos-legacy'), 'pt-BR', TranscriptProviderType.THREE_PLAY_MEDIA)
        make_transcript(make_video('oos-done'), 'pt-br', TranscriptProviderType.THREE_PLAY_MEDIA)

        output = run()

        self.assertIn(f'{TranscriptProviderType.THREE_PLAY_MEDIA}: 1', output)


class DuplicateHandlingTests(TestCase):
    """Collisions, which only arise for mappings that differ by more than case."""

    def test_duplicates_are_reported_and_not_deleted_by_default(self):
        video = make_video('v8')
        legacy = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        incumbent = make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)

        output = run(commit=True)

        self.assertTrue(VideoTranscript.objects.filter(id=legacy.id).exists())
        self.assertTrue(VideoTranscript.objects.filter(id=incumbent.id).exists())
        legacy.refresh_from_db()
        self.assertEqual(legacy.language_code, 'es', 'collision must not rename')
        self.assertIn('duplicate:', output)
        self.assertIn('--resolve-duplicates', output)

    def test_resolve_keeps_the_human_uploaded_transcript(self):
        """A Custom transcript is hand-authored and cannot be regenerated; it must win."""
        video = make_video('v9')
        legacy_ai = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        human = make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        human_file = human.transcript.name

        run(commit=True, resolve_duplicates=True)

        self.assertFalse(VideoTranscript.objects.filter(id=legacy_ai.id).exists())
        human.refresh_from_db()
        self.assertEqual(human.language_code, 'es-419')
        self.assertTrue(human.transcript.storage.exists(human_file), 'kept file must survive')

    def test_resolve_keeps_human_transcript_even_when_it_is_the_legacy_row(self):
        video = make_video('v10')
        human_legacy = make_transcript(video, 'es', TranscriptProviderType.CUSTOM)
        ai_incumbent = make_transcript(video, 'es-419', TranscriptProviderType.EDX_AI_TRANSLATIONS)

        # Custom is out of scope by default, so widening is required to reach this row.
        run(commit=True, resolve_duplicates=True, providers=[TranscriptProviderType.CUSTOM])

        self.assertFalse(VideoTranscript.objects.filter(id=ai_incumbent.id).exists())
        human_legacy.refresh_from_db()
        self.assertEqual(human_legacy.language_code, 'es-419')

    def test_resolve_falls_back_to_most_recent_between_same_kinds(self):
        video = make_video('v11')
        older = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        newer = make_transcript(video, 'es-419', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        VideoTranscript.objects.filter(id=older.id).update(modified='2020-01-01T00:00:00Z')

        run(commit=True, resolve_duplicates=True)

        self.assertFalse(VideoTranscript.objects.filter(id=older.id).exists())
        self.assertTrue(VideoTranscript.objects.filter(id=newer.id).exists())

    def test_discarded_file_is_removed(self):
        video = make_video('v12')
        loser = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        loser_file = loser.transcript.name
        storage = loser.transcript.storage

        # Storage is not transactional, so the command defers file deletion to on_commit;
        # TestCase never commits, so the callbacks have to be run explicitly.
        with self.captureOnCommitCallbacks(execute=True):
            run(commit=True, resolve_duplicates=True)

        self.assertFalse(storage.exists(loser_file))

    def test_file_survives_if_the_deletion_is_rolled_back(self):
        """
        Deleting the file inline would strand the row: a rollback restores the database but
        cannot restore storage. Nothing should be removed unless the transaction commits.
        """
        video = make_video('v12b')
        loser = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        loser_file = loser.transcript.name
        storage = loser.transcript.storage

        # Capture the callbacks without executing them: the "transaction" never commits.
        with self.captureOnCommitCallbacks(execute=False):
            run(commit=True, resolve_duplicates=True)

        self.assertTrue(storage.exists(loser_file))

    def test_shared_file_is_not_removed(self):
        """Two rows pointing at one object must not have it deleted out from under them."""
        video = make_video('v13')
        loser = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        keeper = make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        shared_name = loser.transcript.name
        VideoTranscript.objects.filter(id=keeper.id).update(transcript=shared_name)

        with self.captureOnCommitCallbacks(execute=True):
            run(commit=True, resolve_duplicates=True)

        self.assertTrue(loser.transcript.storage.exists(shared_name))


class ProviderScopeTests(TestCase):
    """
    The legacy codes are not unique to this pipeline. 3PlayMedia's plan legitimately uses
    'de', 'es', 'it', 'ko' and 'tr', so those rows are current vendor data rather than
    pre-cutover artifacts and must not be rewritten or deleted.
    """

    def test_third_party_transcripts_are_not_renamed(self):
        for provider in (TranscriptProviderType.THREE_PLAY_MEDIA, TranscriptProviderType.CIELO24):
            transcript = make_transcript(make_video(f'tp-{provider}'), 'es', provider)

            run(commit=True, resolve_duplicates=True)

            transcript.refresh_from_db()
            self.assertEqual(transcript.language_code, 'es', f'{provider} data must be untouched')

    def test_custom_transcripts_are_not_renamed_by_default(self):
        """'Custom' covers human uploads and pre-Nov-2024 pipeline output alike."""
        transcript = make_transcript(make_video('custom'), 'ko', TranscriptProviderType.CUSTOM)

        run(commit=True, resolve_duplicates=True)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'ko')

    def test_out_of_scope_rows_are_reported(self):
        make_transcript(make_video('tp2'), 'it', TranscriptProviderType.THREE_PLAY_MEDIA)

        output = run()

        self.assertIn('Out of scope', output)
        self.assertIn(TranscriptProviderType.THREE_PLAY_MEDIA, output)

    def test_out_of_scope_tally_counts_rows_not_distinct_pairs(self):
        """
        The --course filter joins through CourseVideo and needs .distinct() to drop the
        repeats that join produces. Selecting only (provider, language_code) lets DISTINCT
        fold every row sharing a pair into one, reporting 1 for any number of rows.
        """
        for index in range(3):
            make_transcript(make_video(f'oos{index}'), 'it', TranscriptProviderType.THREE_PLAY_MEDIA)

        output = run(courses=[COURSE_ID])

        self.assertIn(f'{TranscriptProviderType.THREE_PLAY_MEDIA}: 3', output)

    def test_out_of_scope_tally_counts_a_shared_video_once(self):
        """The .distinct() still has to do its job: two matching courses, one row, one count."""
        video = make_video('oos-multi')
        CourseVideo.objects.create(video=video, course_id=OTHER_COURSE_ID)
        make_transcript(video, 'it', TranscriptProviderType.THREE_PLAY_MEDIA)

        output = run(courses=[COURSE_ID, OTHER_COURSE_ID])

        self.assertIn(f'{TranscriptProviderType.THREE_PLAY_MEDIA}: 1', output)

    def test_include_provider_widens_scope(self):
        transcript = make_transcript(make_video('widen'), 'it', TranscriptProviderType.CUSTOM)

        run(commit=True, providers=[TranscriptProviderType.CUSTOM])

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'it-it')

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            run(providers=['NotAProvider'])
        self.assertIn('NotAProvider', str(ctx.exception))

    def test_newer_ai_row_never_displaces_an_older_vendor_row(self):
        """Recency must not let a cheap machine translation beat a paid transcription."""
        video = make_video('guard')
        ours = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        theirs = make_transcript(video, 'es-419', TranscriptProviderType.THREE_PLAY_MEDIA)
        VideoTranscript.objects.filter(id=theirs.id).update(modified='2020-01-01T00:00:00Z')

        run(commit=True, resolve_duplicates=True)

        # The vendor row is older but wins on provider tier, so ours is the one dropped.
        theirs.refresh_from_db()
        self.assertEqual(theirs.language_code, 'es-419')
        self.assertEqual(theirs.provider, TranscriptProviderType.THREE_PLAY_MEDIA)
        self.assertFalse(VideoTranscript.objects.filter(id=ours.id).exists())

    def test_will_not_delete_another_providers_transcript_to_resolve(self):
        """When the losing row belongs to a provider out of scope, nothing is deleted."""
        video = make_video('guard2')
        # Custom outranks 3PlayMedia, so the vendor row would be the loser -- but this run
        # has no claim over it, so the pair is reported instead.
        ours = make_transcript(video, 'es', TranscriptProviderType.CUSTOM)
        theirs = make_transcript(video, 'es-419', TranscriptProviderType.THREE_PLAY_MEDIA)

        output = run(commit=True, resolve_duplicates=True, providers=[TranscriptProviderType.CUSTOM])

        self.assertTrue(VideoTranscript.objects.filter(id=theirs.id).exists())
        ours.refresh_from_db()
        self.assertEqual(ours.language_code, 'es')
        self.assertIn('other provider owns the canonical code', output)

    def test_our_row_may_still_lose_to_another_provider(self):
        """The guard protects vendor rows from deletion, not ours from losing."""
        video = make_video('lose')
        ours = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        theirs = make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)

        run(commit=True, resolve_duplicates=True)

        self.assertFalse(VideoTranscript.objects.filter(id=ours.id).exists())
        self.assertTrue(VideoTranscript.objects.filter(id=theirs.id).exists())


class DryRunAccuracyTests(TestCase):
    """A dry run has to predict what a committed run would actually do."""

    def test_dry_run_reports_collisions(self):
        video = make_video('dry1')
        make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)

        output = run()

        self.assertIn('duplicate:', output)

    def test_dry_run_totals_match_a_committed_run(self):
        video = make_video('dry2')
        make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        make_transcript(make_video('dry3'), 'it')

        predicted = run(resolve_duplicates=True)
        with self.captureOnCommitCallbacks(execute=True):
            actual = run(commit=True, resolve_duplicates=True)

        # 'it' renames cleanly; the 'es' pair resolves by dropping our row.
        self.assertIn('Would rename 1 transcript(s); would remove 1 duplicate(s); skipped 0.', predicted)
        self.assertIn('Renamed 1 transcript(s); removed 1 duplicate(s); skipped 0.', actual)

    def test_dry_run_writes_nothing_even_with_resolve_duplicates(self):
        video = make_video('dry4')
        legacy = make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        incumbent = make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)
        other = make_transcript(make_video('dry5'), 'it')

        run(resolve_duplicates=True)

        self.assertTrue(VideoTranscript.objects.filter(id=legacy.id).exists())
        self.assertTrue(VideoTranscript.objects.filter(id=incumbent.id).exists())
        other.refresh_from_db()
        self.assertEqual(other.language_code, 'it')

    def test_dry_run_counts_skips_when_not_resolving(self):
        video = make_video('dry6')
        make_transcript(video, 'es', TranscriptProviderType.EDX_AI_TRANSLATIONS)
        make_transcript(video, 'es-419', TranscriptProviderType.CUSTOM)

        output = run()

        self.assertIn('Would rename 0 transcript(s); would remove 0 duplicate(s); skipped 1.', output)


class CourseScopeTests(TestCase):
    """--course selection and validation."""

    def test_scope_limits_the_rewrite(self):
        in_scope = make_transcript(make_video('in', COURSE_ID), 'de')
        other = make_transcript(make_video('out', 'course-v1:edX+Other+2024'), 'de')

        run(commit=True, courses=[COURSE_ID])

        in_scope.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(in_scope.language_code, 'de-de')
        self.assertEqual(other.language_code, 'de', 'out-of-scope transcript must be untouched')

    def test_known_course_with_no_transcripts_is_accepted(self):
        """A real course that simply has nothing to backfill is not an error."""
        make_video('no-transcripts', COURSE_ID)

        output = run(courses=[COURSE_ID])

        self.assertIn('DRY-RUN', output)

    def test_unknown_course_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            run(courses=['course-v1:edX+Nope+2024'])
        self.assertIn('course-v1:edX+Nope+2024', str(ctx.exception))

    def test_invalid_batch_size_is_rejected(self):
        with self.assertRaises(CommandError):
            run(batch_size=0)


class StaleSnapshotTests(TestCase):
    """
    The id scan runs once per language, so a candidate can change before its turn.
    `create_or_update` overwrites language_code and provider in place, and Studio exposes
    that as a normal authoring action, so this is a supported operation racing the backfill
    -- and it produces no unique-key conflict to catch.
    """

    @staticmethod
    def _run_with_interference(transcript, **changes):
        """
        Apply `changes` to `transcript` in the window Copilot identified: after the id scan
        that selects candidates, but before the row is processed. The row is still fetched
        by id into the batch, so only the per-row recheck can catch it.
        """
        original = Command._exact_match_ids

        def scan_then_interfere(command, course_ids, providers, code):
            ids = original(command, course_ids, providers, code)
            # The scan runs once per code and its result is handed straight to _rewrite, so
            # interfering here leaves the id in the batch: the row is still fetched by id and
            # only the per-row recheck can stop it.
            if transcript.id in ids:
                VideoTranscript.objects.filter(id=transcript.id).update(**changes)
            return ids

        Command._exact_match_ids = scan_then_interfere
        try:
            return run(commit=True, resolve_duplicates=True)
        finally:
            Command._exact_match_ids = original

    def test_language_changed_under_us_is_left_alone(self):
        """An author's deliberate re-language must not be clobbered by the backfill."""
        transcript = make_transcript(make_video('race-lang'), 'es')

        output = self._run_with_interference(transcript, language_code='fr')

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'fr', "author's change must survive")
        self.assertIn('changed under us', output)

    def test_provider_changed_under_us_is_left_alone(self):
        transcript = make_transcript(make_video('race-provider'), 'es')

        output = self._run_with_interference(transcript, provider=TranscriptProviderType.THREE_PLAY_MEDIA)

        transcript.refresh_from_db()
        self.assertEqual(transcript.language_code, 'es', 'row left scope; must not be renamed')
        self.assertIn('changed under us', output)

    def test_row_deleted_under_us_is_not_an_error(self):
        transcript = make_transcript(make_video('race-delete'), 'es')
        other = make_transcript(make_video('race-other'), 'es')

        original = Command._exact_match_ids

        def scan_then_delete(command, course_ids, providers, code):
            ids = original(command, course_ids, providers, code)
            if transcript.id in ids:
                VideoTranscript.objects.filter(id=transcript.id).delete()
            return ids

        Command._exact_match_ids = scan_then_delete
        try:
            output = run(commit=True)
        finally:
            Command._exact_match_ids = original

        self.assertIn('finished', output)
        other.refresh_from_db()
        self.assertEqual(other.language_code, 'es-419', 'the rest of the run must continue')


class ConcurrencyTests(TestCase):
    """A live pipeline can create a canonical row after the batch snapshot is taken."""

    def test_concurrent_row_skips_one_transcript_not_the_run(self):
        racer = make_transcript(make_video('race'), 'de')
        other = make_transcript(make_video('fine'), 'de')

        original_rename = type(racer).save

        def rename_with_interference(self, *args, **kwargs):
            # Simulate another writer claiming (video, 'de-de') between snapshot and rename.
            if self.id == racer.id and self.language_code == 'de-de':
                VideoTranscript.objects.filter(id=self.id).update(language_code='de')
                VideoTranscript.objects.create(
                    video=racer.video, language_code='de-de', file_format='srt',
                )
            return original_rename(self, *args, **kwargs)

        with self.settings():
            type(racer).save = rename_with_interference
            try:
                output = run(commit=True)
            finally:
                type(racer).save = original_rename

        # The run completed and the unaffected transcript was still processed.
        self.assertIn('finished', output)
        other.refresh_from_db()
        self.assertEqual(other.language_code, 'de-de')
