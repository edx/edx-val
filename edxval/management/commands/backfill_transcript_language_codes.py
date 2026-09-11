"""
Management command to normalize VideoTranscript language codes to canonical edX codes.

Transcripts registered before the edX language-code cutover were stored under
provider-specific codes ("pt-BR", "zh-CN", "es"). When canonical rows ("pt-br", "zh-cn",
"es-419") are later added for the same video, a player's caption picker renders both: they
are distinct `language_code` values, but they resolve to the same display label, so the
learner sees two identical adjacent entries.

Run it from any project that installs this app, e.g. from Studio in edx-platform:

    python manage.py cms backfill_transcript_language_codes
    python manage.py cms backfill_transcript_language_codes --commit
    python manage.py cms backfill_transcript_language_codes --course course-v1:X+Y+Z --commit
    python manage.py cms backfill_transcript_language_codes --commit --resolve-duplicates

Nothing is deleted by default. Where renaming would collide with an existing transcript for
the same video, the pair is reported and the legacy row is left alone; pass
`--resolve-duplicates` to let the command delete the losing row and its file.

Two notes on scope:

* Transcript files are stored under uuid-based, language-agnostic keys
  ("<prefix><uuid4hex>.<ext>"), so renaming `language_code` in the database does not orphan
  the stored file. The rename is applied with a direct field save rather than
  `VideoTranscript.create_or_update`, whose `save_transcript` mints a fresh uuid filename
  when called without file data and would strand the real object in storage.
* This table also holds non-canonical codes that never came from the translation pipeline
  (third-party transcription plans contribute "zh-tw", "zh-cmn", "zh-yue", and hand-uploads
  can contribute anything). Those are deliberately left alone; mapping them needs its own
  product decision.

This command only touches VideoTranscript rows. A course authoring system may hold its own
copy of the transcript language list (edx-platform keeps one in `VideoBlock.transcripts`),
and duplicates can persist there until it is normalized too.
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from edxval.models import CourseVideo, TranscriptProviderType, VideoTranscript

logger = logging.getLogger(__name__)

# Legacy (provider-format) code -> canonical edX code.
#
# Kept in sync by hand with ai-translations, which derives the same map by inverting its
# edX->GCP mapping table (see its backfill_language_codes command). This package cannot
# import that module.
#
# "fa" is intentionally absent: it is ambiguous between "fa" and "fa-ir", but is itself a
# valid canonical code, so existing rows are already correct.
# "es" is ambiguous between "es-419" and "es-es"; it resolves to "es-419" because the large
# majority of historical edX Spanish content targets Latin American Spanish.
LEGACY_TO_CANONICAL = {
    'de': 'de-de',
    'es': 'es-419',
    'fr-CA': 'fr-ca',
    'it': 'it-it',
    'ko': 'ko-kr',
    'pt-BR': 'pt-br',
    'pt-PT': 'pt-pt',
    'tr': 'tr-tr',
    'zh-CN': 'zh-cn',
}


def _chunked(items, size):
    """Yield successive slices of `items` of at most `size` elements."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Command(BaseCommand):
    """
    Rewrite legacy VideoTranscript language codes to canonical edX codes.
    """
    help = 'Normalize edx-val VideoTranscript language codes to canonical edX codes.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            default=False,
            help='Apply the changes. Without this flag the command only reports what it would do.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Number of transcripts to load and process per transaction.',
        )
        parser.add_argument(
            '--course',
            action='append',
            dest='courses',
            default=None,
            help='Limit to videos in this course (repeatable). Defaults to every video.',
        )
        parser.add_argument(
            '--resolve-duplicates',
            action='store_true',
            default=False,
            help=(
                'Delete a transcript when renaming it would collide with an existing one. '
                'Without this flag collisions are only reported, and nothing is deleted.'
            ),
        )

    def handle(self, *args, **options):
        commit = options['commit']
        batch_size = options['batch_size']
        resolve_duplicates = options['resolve_duplicates']
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')

        course_ids = self._validate_course_ids(options['courses'])
        mode = 'COMMIT' if commit else 'DRY-RUN'
        scope = ', '.join(course_ids) if course_ids else 'all courses'
        self.stdout.write(f'VideoTranscript language backfill starting in [{mode}] mode ({scope}).')
        if commit and not resolve_duplicates:
            self.stdout.write('Collisions will be reported only; pass --resolve-duplicates to delete.')

        renamed_total = 0
        deleted_total = 0
        skipped_total = 0
        for legacy_code, canonical_code in LEGACY_TO_CANONICAL.items():
            count = len(self._exact_match_ids(course_ids, legacy_code))
            if not count:
                continue

            self.stdout.write(f"  '{legacy_code}' -> '{canonical_code}': {count} transcript(s)")
            if not commit:
                continue

            renamed, deleted, skipped = self._rewrite(
                legacy_code,
                canonical_code,
                course_ids=course_ids,
                batch_size=batch_size,
                resolve_duplicates=resolve_duplicates,
            )
            renamed_total += renamed
            deleted_total += deleted
            skipped_total += skipped
            self.stdout.write(f'    renamed {renamed}, removed {deleted}, skipped {skipped}')

        verb = 'Renamed' if commit else 'Would rename'
        self.stdout.write(
            self.style.SUCCESS(
                f'[{mode}] finished. {verb} {renamed_total} transcript(s); '
                f'removed {deleted_total} duplicate(s); skipped {skipped_total}.'
            )
        )
        if skipped_total and not resolve_duplicates:
            self.stdout.write(
                self.style.WARNING(
                    f'{skipped_total} transcript(s) left unrenamed because of collisions. '
                    'Review the duplicates above, then re-run with --resolve-duplicates.'
                )
            )

    @staticmethod
    def _validate_course_ids(course_ids):
        """
        Reject --course values this app has never seen, so a typo fails fast instead of
        silently backfilling nothing.

        Membership is checked against CourseVideo rather than VideoTranscript: a real course
        may simply have no transcripts yet, and rejecting it as "not found" would be wrong.
        `CourseVideo.course_id` is an opaque CharField here, so values are matched as plain
        strings; this package does not depend on opaque-keys.
        """
        if not course_ids:
            return []

        known = set(
            CourseVideo.objects.filter(course_id__in=course_ids).values_list('course_id', flat=True)
        )
        unknown = [course_id for course_id in course_ids if course_id not in known]
        if unknown:
            raise CommandError(f"No videos found in edx-val for course(s): {', '.join(unknown)}")
        return course_ids

    @staticmethod
    def _base_queryset(course_ids):
        """All transcripts in scope."""
        queryset = VideoTranscript.objects.all()
        if course_ids:
            queryset = queryset.filter(video__courses__course_id__in=course_ids).distinct()
        return queryset

    def _exact_match_ids(self, course_ids, code):
        """
        Ids of in-scope transcripts holding exactly `code`, compared case-sensitively.

        MySQL's default collation is case-insensitive, so `filter(language_code='pt-BR')`
        also returns rows already stored as 'pt-br' (and vice versa). Four of the nine
        mappings differ only by case, so trusting the database comparison would make a row
        look like its own duplicate: it would be deleted, and the rename that followed would
        match no rows. The database narrows the candidates; Python decides case-sensitively.

        Materializing the ids also bounds the work, so the caller iterates a fixed list
        rather than re-querying a predicate a case-insensitive collation keeps matching.
        """
        candidates = self._base_queryset(course_ids).filter(language_code=code).values_list('id', 'language_code')
        return [transcript_id for transcript_id, value in candidates if value == code]

    def _rewrite(self, legacy_code, canonical_code, *, course_ids, batch_size, resolve_duplicates):
        """
        Rename every legacy-coded transcript in scope, reporting or resolving duplicates.

        `VideoTranscript.video` is nullable and MySQL treats NULLs as distinct in a unique
        index, so orphaned rows cannot collide and are renamed unconditionally.

        Note that the same case-insensitive collation makes a case-only duplicate pair
        impossible on this table: `('v', 'pt-BR')` and `('v', 'pt-br')` violate
        `unique_together`, so the database would already have rejected the second row.
        Genuine collisions only arise for mappings that differ by more than case.
        """
        renamed_total = 0
        deleted_total = 0
        skipped_total = 0

        for chunk in _chunked(self._exact_match_ids(course_ids, legacy_code), batch_size):
            batch = list(VideoTranscript.objects.filter(id__in=chunk))
            attached_video_ids = [t.video_id for t in batch if t.video_id is not None]
            incumbents = {
                transcript.video_id: transcript
                for transcript in VideoTranscript.objects.filter(
                    video_id__in=attached_video_ids,
                    language_code=canonical_code,
                ).exclude(id__in=chunk)
                if transcript.language_code == canonical_code
            }

            for transcript in batch:
                incumbent = incumbents.get(transcript.video_id) if transcript.video_id is not None else None
                renamed, deleted, skipped = self._process_one(
                    transcript, incumbent, canonical_code, resolve_duplicates,
                )
                renamed_total += renamed
                deleted_total += deleted
                skipped_total += skipped

        return renamed_total, deleted_total, skipped_total

    def _process_one(self, transcript, incumbent, canonical_code, resolve_duplicates):
        """
        Rename a single transcript, or report/resolve its collision.

        Returns a (renamed, deleted, skipped) tally. `IntegrityError` is caught rather than
        allowed to abort the run: the incumbent snapshot can go stale while a live pipeline
        keeps writing, and one concurrent insert should cost one transcript, not the backfill.
        """
        if incumbent is not None and not resolve_duplicates:
            self._report_duplicate(transcript, incumbent)
            return 0, 0, 1

        try:
            with transaction.atomic():
                if incumbent is None:
                    self._rename(transcript, canonical_code)
                    return 1, 0, 0

                if self._preferred(transcript, incumbent) is incumbent:
                    self._discard(transcript, kept=incumbent)
                    return 0, 1, 0

                self._discard(incumbent, kept=transcript)
                self._rename(transcript, canonical_code)
                return 1, 1, 0
        except IntegrityError:
            logger.warning(
                'backfill_transcript_language_codes: skipping VideoTranscript %s; a row for '
                '(video=%s, language=%s) was created concurrently.',
                transcript.id, transcript.video_id, canonical_code,
            )
            self.stdout.write(
                self.style.WARNING(f'    skipped transcript {transcript.id} (conflicting row created concurrently)')
            )
            return 0, 0, 1

    def _report_duplicate(self, transcript, incumbent):
        """Describe a collision precisely enough to act on without re-querying."""
        message = (
            f'    duplicate: video={transcript.video_id} '
            f'keeps id={incumbent.id} ({incumbent.language_code}, {incumbent.provider}, '
            f'modified {incumbent.modified:%Y-%m-%d}) / '
            f'conflicts with id={transcript.id} ({transcript.language_code}, {transcript.provider}, '
            f'modified {transcript.modified:%Y-%m-%d})'
        )
        logger.info('backfill_transcript_language_codes: %s', message.strip())
        self.stdout.write(self.style.WARNING(message))

    @staticmethod
    def _preferred(left, right):
        """
        Choose which of two transcripts for the same video and language to keep.

        A hand-uploaded "Custom" transcript wins: it is the only kind a human authored or
        corrected, and it cannot be regenerated, whereas a pipeline transcript can. Between
        two of the same kind the most recently modified wins.
        """
        def rank(transcript):
            return (transcript.provider != TranscriptProviderType.CUSTOM, -transcript.modified.timestamp())

        return left if rank(left) <= rank(right) else right

    @staticmethod
    def _rename(transcript, canonical_code):
        """Rename in place. Deliberately not via create_or_update, which would re-key the file."""
        transcript.language_code = canonical_code
        transcript.save(update_fields=['language_code'])

    def _discard(self, transcript, kept):
        """
        Delete a duplicate transcript, then its stored file.

        Storage is not transactional, so the file is deleted only once the surrounding
        transaction commits. Deleting it inline would leave the file gone but the row intact
        if anything later in the transaction failed. The file is also kept if any other row
        still points at it.
        """
        file_name = transcript.transcript.name if transcript.transcript else None
        storage = transcript.transcript.storage if file_name else None
        logger.info(
            'backfill_transcript_language_codes: discarding VideoTranscript %s (video=%s, language=%s, '
            'provider=%s) in favour of %s (language=%s, provider=%s)',
            transcript.id, transcript.video_id, transcript.language_code, transcript.provider,
            kept.id, kept.language_code, kept.provider,
        )
        transcript.delete()

        if not file_name:
            return
        transaction.on_commit(lambda: self._delete_file(storage, file_name))

    @staticmethod
    def _delete_file(storage, file_name):
        """Remove an orphaned transcript file, never letting storage errors abort the run."""
        if VideoTranscript.objects.filter(transcript=file_name).exists():
            logger.info('Keeping transcript file %s; still referenced by another row.', file_name)
            return
        try:
            storage.delete(file_name)
        except Exception:  # pylint: disable=broad-except
            logger.exception('Could not delete transcript file %s', file_name)
