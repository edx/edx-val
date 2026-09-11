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
from django.db import transaction

from edxval.models import TranscriptProviderType, VideoTranscript

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

    def handle(self, *args, **options):
        commit = options['commit']
        batch_size = options['batch_size']
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')

        course_ids = self._validate_course_ids(options['courses'])
        mode = 'COMMIT' if commit else 'DRY-RUN'
        scope = ', '.join(course_ids) if course_ids else 'all courses'
        self.stdout.write(f'VideoTranscript language backfill starting in [{mode}] mode ({scope}).')

        renamed_total = 0
        deleted_total = 0
        for legacy_code, canonical_code in LEGACY_TO_CANONICAL.items():
            count = self._base_queryset(course_ids).filter(language_code=legacy_code).count()
            if not count:
                continue

            self.stdout.write(f"  '{legacy_code}' -> '{canonical_code}': {count} transcript(s)")
            if not commit:
                continue

            renamed, deleted = self._rewrite(legacy_code, canonical_code, course_ids, batch_size)
            renamed_total += renamed
            deleted_total += deleted
            self.stdout.write(f'    renamed {renamed}, removed {deleted} duplicate(s)')

        verb = 'Renamed' if commit else 'Would rename'
        self.stdout.write(
            self.style.SUCCESS(
                f'[{mode}] finished. {verb} {renamed_total} transcript(s); '
                f'removed {deleted_total} duplicate(s).'
            )
        )

    def _validate_course_ids(self, course_ids):
        """
        Check that each --course value actually matches a course, so a typo surfaces as an
        error rather than as a silent no-op backfill.

        `CourseVideo.course_id` is an opaque CharField in this app, so the values are
        matched as plain strings; this package does not depend on opaque-keys.
        """
        if not course_ids:
            return []

        unknown = [
            course_id
            for course_id in course_ids
            if not VideoTranscript.objects.filter(video__courses__course_id=course_id).exists()
        ]
        if unknown:
            raise CommandError(f"No video transcripts found for course(s): {', '.join(unknown)}")
        return course_ids

    @staticmethod
    def _base_queryset(course_ids):
        """All transcripts in scope."""
        queryset = VideoTranscript.objects.all()
        if course_ids:
            queryset = queryset.filter(video__courses__course_id__in=course_ids).distinct()
        return queryset

    def _rewrite(self, legacy_code, canonical_code, course_ids, batch_size):
        """
        Rename every legacy-coded transcript in scope, resolving duplicates as they appear.

        Two queries per batch: one for the legacy rows, one for any canonical rows already
        occupying `(video, canonical_code)`. `unique_together` guarantees at most one
        canonical row per video, so the collision lookup collapses to a dict.

        `VideoTranscript.video` is nullable, and MySQL treats NULLs as distinct in a unique
        index, so orphaned rows cannot collide with anything and are renamed unconditionally.
        """
        renamed_total = 0
        deleted_total = 0

        while True:
            batch = list(self._base_queryset(course_ids).filter(language_code=legacy_code)[:batch_size])
            if not batch:
                break

            attached_video_ids = [transcript.video_id for transcript in batch if transcript.video_id is not None]
            incumbents = {
                transcript.video_id: transcript
                for transcript in VideoTranscript.objects.filter(
                    video_id__in=attached_video_ids,
                    language_code=canonical_code,
                )
            }

            for transcript in batch:
                incumbent = incumbents.get(transcript.video_id) if transcript.video_id is not None else None
                with transaction.atomic():
                    if incumbent is None:
                        self._rename(transcript, canonical_code)
                        renamed_total += 1
                        continue

                    if self._preferred(transcript, incumbent) is incumbent:
                        self._discard(transcript, kept=incumbent)
                        deleted_total += 1
                    else:
                        self._discard(incumbent, kept=transcript)
                        deleted_total += 1
                        self._rename(transcript, canonical_code)
                        renamed_total += 1

        return renamed_total, deleted_total

    @staticmethod
    def _preferred(left, right):
        """
        Choose which of two transcripts for the same video and language to keep.

        A provider-generated transcript beats a hand-uploaded "Custom" one, since the former
        came from the transcription or translation pipeline. Otherwise the most recently
        modified wins.
        """
        def rank(transcript):
            return (transcript.provider == TranscriptProviderType.CUSTOM, -transcript.modified.timestamp())

        return left if rank(left) <= rank(right) else right

    @staticmethod
    def _rename(transcript, canonical_code):
        """Rename in place. Deliberately not via edxval.api, which would re-key the file."""
        transcript.language_code = canonical_code
        transcript.save(update_fields=['language_code'])

    def _discard(self, transcript, kept):
        """
        Delete a duplicate transcript and its stored file.

        The file is removed only when no other row points at it, so a shared name can never
        be pulled out from under a transcript that is being kept.
        """
        file_name = transcript.transcript.name if transcript.transcript else None
        logger.info(
            'backfill_transcript_language_codes: discarding VideoTranscript %s (video=%s, language=%s, '
            'provider=%s) in favour of %s (language=%s, provider=%s)',
            transcript.id, transcript.video_id, transcript.language_code, transcript.provider,
            kept.id, kept.language_code, kept.provider,
        )
        transcript.delete()

        if not file_name:
            return
        if VideoTranscript.objects.filter(transcript=file_name).exists():
            logger.info('Keeping transcript file %s; still referenced by another row.', file_name)
            return
        try:
            transcript.transcript.delete(save=False)
        except Exception:  # pylint: disable=broad-except
            # A missing or unreachable object should not abort the backfill.
            logger.exception('Could not delete transcript file %s', file_name)
