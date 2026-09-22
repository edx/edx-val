"""
Fold legacy VideoTranscript.language_code spellings into their canonical edX codes.

A second pass over the column 0006 corrected, covering spellings that predate the GCP round
trip rather than being caused by it: BCP 47 regional tags ("en-US", "en-GB"), the deprecated
ISO 639-1 code for Hebrew ("iw", superseded by "he" in 1989), and three ways of writing
Simplified Chinese ("zh-Hans-CN", "zh_HANS", "zh-Hans") that the rest of the platform spells
"zh-cn".

Same symptom as 0006: a transcript's display label falls back to the generic part of its
code, so a video carrying both "en-US" and "en" lists English twice in the caption picker.

"en-GB" folds into "en", not into "uk" -- "uk" is Ukrainian in edX's language list, and
British English has no separate code.

Where a rename would land on a code the video already holds, the legacy row is deleted
instead: that pair is the duplicate described above, and unique_together on
(video, language_code) leaves nowhere to rename it to. The same applies when two legacy
spellings on one video fold into the same code. These deletions are permanent, which is why
this migration does not reverse; see `_irreversible`.

Unlike 0006, the legacy spellings are matched case-insensitively. None of them differ from
their target by case alone, so there is no canonical row for a loose match to sweep up, and
"en-us" is exactly as wrong as "en-US". It also makes MySQL and SQLite agree, so the tests
exercise what production will do.
"""

import logging
from collections import defaultdict

from django.db import migrations

logger = logging.getLogger(__name__)

MIGRATION = "0007_backfill_language_code_legacy_to_edx"

# A single language code can cover far more rows than belong in one IN clause.
BATCH_SIZE = 1000

# Ordered: where two spellings fold into the same code, the one listed first wins a video
# that holds both.
LEGACY_TO_EDX = {
    "en-US": "en",
    "en-GB": "en",
    "iw": "he",
    "zh-Hans-CN": "zh-cn",
    "zh_HANS": "zh-cn",
    "zh-Hans": "zh-cn",
}


def _legacy_rows(VideoTranscript, db_alias, legacy_code):
    """
    Return [(id, video_id), ...] for the rows holding legacy_code in any capitalisation.

    Ordered by id so that batching is stable and the row a video keeps is the oldest one.
    """
    return list(
        VideoTranscript.objects.using(db_alias)
        .filter(language_code__iexact=legacy_code)
        .order_by("id")
        .values_list("id", "video_id")
    )


def _fold_batch(VideoTranscript, db_alias, legacy_code, edx_code, batch):
    """
    Rename one batch of rows, deleting any that would collide instead.

    Each deletion is logged first, because it cannot be undone.
    """
    # Rows with no video are renamed unconditionally -- they can never collide, because a
    # unique index treats NULLs as distinct.
    rename_ids = set()
    stale_ids = set()

    rows_by_video = defaultdict(list)
    for row_id, video_id in batch:
        if video_id is None:
            rename_ids.add(row_id)
        else:
            rows_by_video[video_id].append(row_id)

    # Only reachable under a case-sensitive collation -- MySQL's unique index already rules
    # out "zh-Hans" alongside "zh-hans" on one video.
    keeper_by_video = {}
    for video_id, row_ids in rows_by_video.items():
        keeper_by_video[video_id] = row_ids[0]
        for row_id in row_ids[1:]:
            stale_ids.add(row_id)
            logger.warning(
                "%s: dropping row %s for video %s; its %r already folds into %r. "
                "This deletion is not reversible.",
                MIGRATION, row_id, video_id, legacy_code, edx_code,
            )

    # Matched with `=`, not iexact, so the notion of a collision is the collation's own --
    # the same one the unique index enforces.
    incumbents = (
        VideoTranscript.objects.using(db_alias)
        .filter(language_code=edx_code, video_id__in=list(keeper_by_video))
        .values_list("video_id", "provider")
    )
    for video_id, provider in incumbents:
        blocked_id = keeper_by_video.pop(video_id, None)
        if blocked_id is None:
            continue
        stale_ids.add(blocked_id)
        logger.warning(
            "%s: dropping stale %r row for video %s; a %s transcript already holds %r. "
            "This deletion is not reversible.",
            MIGRATION, legacy_code, video_id, provider, edx_code,
        )

    rename_ids.update(keeper_by_video.values())
    if rename_ids:
        VideoTranscript.objects.using(db_alias).filter(
            id__in=rename_ids,
        ).update(language_code=edx_code)

    if stale_ids:
        VideoTranscript.objects.using(db_alias).filter(id__in=stale_ids).delete()
        logger.info(
            "%s: deleted %d stale %r row(s) superseded by %r.",
            MIGRATION, len(stale_ids), legacy_code, edx_code,
        )


def _fold_language_code(VideoTranscript, db_alias, legacy_code, edx_code):
    """Rename every row holding legacy_code to edx_code, in batches."""
    rows = _legacy_rows(VideoTranscript, db_alias, legacy_code)
    for start in range(0, len(rows), BATCH_SIZE):
        _fold_batch(
            VideoTranscript, db_alias, legacy_code, edx_code, rows[start:start + BATCH_SIZE],
        )


def legacy_to_edx_language_codes(apps, schema_editor):
    """Fold every legacy language code spelling into its canonical edX equivalent."""
    VideoTranscript = apps.get_model("edxval", "VideoTranscript")
    db_alias = schema_editor.connection.alias

    for legacy_code, edx_code in LEGACY_TO_EDX.items():
        _fold_language_code(VideoTranscript, db_alias, legacy_code, edx_code)


def _irreversible(apps, schema_editor):  # pylint: disable=unused-argument
    """
    Unapply without touching the data, because a real reverse would do more harm than good.

    Every mapping here is many-to-one, so there is nothing to reverse to, and the rows
    deleted on the way forward are gone either way. Worse, the vast majority of rows now
    holding "en", "he" or "zh-cn" never held a legacy code -- rewriting them to "en-US" and
    "iw" would corrupt far more data than the forward pass ever fixed.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('edxval', '0006_backfill_language_code_gcp_to_edx'),
    ]

    operations = [
        migrations.RunPython(legacy_to_edx_language_codes, reverse_code=_irreversible),
    ]
