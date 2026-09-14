"""
Relabel VideoTranscript.language_code from GCP-format codes to canonical edX codes.

ai-translations used to convert edX codes into GCP codes before writing transcripts back to
edx-val -- fixed going forward by LP-1118 -- so this column holds spellings like "es" and
"pt-BR" where the rest of the platform expects "es-419" and "pt-br". This corrects the rows
already written.

The mismatch is not cosmetic: a transcript's display label falls back to the generic part of
its code, so "pt-br" and "pt-BR" both render as Portuguese, and a video carrying one of each
lists the language twice in the caption picker.

Rows from every provider are relabelled -- the GCP spellings are not valid edX codes for
anyone, whoever wrote them. The one ambiguous mapping is "es": both "es-419" and "es-es"
collapse to "es" on the GCP side, so there is no way to tell which a row started as, and per
product decision it becomes "es-419".

Where a video already holds the canonical code, the row still spelled the GCP way is the
stale copy and is deleted rather than renamed. That pair is the duplicate described above,
and unique_together on (video, language_code) leaves nowhere to rename it to. The deletion is
permanent: reversing this migration does not bring those rows back.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# Caps the size of the id lists sent to the database; a single language code can cover far
# more rows than belong in one IN clause.
BATCH_SIZE = 1000

GCP_TO_EDX = {
    "de": "de-de",
    "fr-CA": "fr-ca",
    "it": "it-it",
    "ko": "ko-kr",
    "pt-BR": "pt-br",
    "pt-PT": "pt-pt",
    "tr": "tr-tr",
    "zh-CN": "zh-cn",
    "es": "es-419",  # ambiguous; see module docstring
}
EDX_TO_GCP = {edx_code: gcp_code for gcp_code, edx_code in GCP_TO_EDX.items()}


def _rows_holding_exactly(VideoTranscript, db_alias, code):
    """
    Return [(id, video_id), ...] for the rows whose language_code is exactly `code`.

    Four of the mappings differ from their target only in case, and MySQL's collation is
    case-insensitive, so the database also returns rows already stored under the target
    spelling. It narrows the candidates; Python decides which ones really match.
    """
    return [
        (row_id, video_id)
        for row_id, video_id, language_code in (
            VideoTranscript.objects.using(db_alias)
            .filter(language_code=code)
            .values_list("id", "video_id", "language_code")
        )
        if language_code == code
    ]


def _backfill_batch(VideoTranscript, db_alias, old_code, new_code, batch):
    """
    Rename one batch of rows, dropping any whose video already holds new_code.

    Such a pair is the duplicate this migration exists to remove: the row holding new_code is
    the canonical one, so the row still spelled old_code is the stale copy and is deleted.
    Each one is logged first, because the deletion cannot be undone on reverse.
    """
    # Keyed by video so an incumbent can be traced back to the row it blocks. Rows with no
    # video are left out because SQL `IN (NULL)` matches nothing; they can never collide --
    # unique indexes treat NULLs as distinct -- so they fall through to the rename below.
    row_id_by_video = {video_id: row_id for row_id, video_id in batch if video_id is not None}

    candidates = (
        VideoTranscript.objects.using(db_alias)
        .filter(language_code=new_code, video_id__in=list(row_id_by_video))
        .values_list("video_id", "provider", "language_code")
    )

    occupied_video_ids = set()
    for video_id, provider, language_code in candidates:
        if language_code != new_code:
            # A case variant the collation matched, including the row being renamed itself.
            continue
        occupied_video_ids.add(video_id)
        logger.warning(
            "0006_backfill_language_code_gcp_to_edx: dropping stale %r row for video %s; a %s "
            "transcript already holds %r. This deletion is not reversible.",
            old_code, video_id, provider, new_code,
        )

    blocked_ids = {row_id_by_video[video_id] for video_id in occupied_video_ids}
    VideoTranscript.objects.using(db_alias).filter(
        id__in={row_id for row_id, _ in batch} - blocked_ids,
    ).update(language_code=new_code)

    if blocked_ids:
        VideoTranscript.objects.using(db_alias).filter(id__in=blocked_ids).delete()
        logger.info(
            "0006_backfill_language_code_gcp_to_edx: deleted %d stale %r row(s) superseded by %r.",
            len(blocked_ids), old_code, new_code,
        )


def _backfill_language_code(VideoTranscript, db_alias, old_code, new_code):
    """Rename every row holding old_code to new_code, in batches."""
    rows = _rows_holding_exactly(VideoTranscript, db_alias, old_code)
    for start in range(0, len(rows), BATCH_SIZE):
        _backfill_batch(
            VideoTranscript, db_alias, old_code, new_code, rows[start:start + BATCH_SIZE],
        )


def gcp_to_edx_language_codes(apps, schema_editor):
    """Relabel every GCP-format language code to its canonical edX equivalent."""
    VideoTranscript = apps.get_model("edxval", "VideoTranscript")
    db_alias = schema_editor.connection.alias

    for gcp_code, edx_code in GCP_TO_EDX.items():
        _backfill_language_code(VideoTranscript, db_alias, gcp_code, edx_code)


def edx_to_gcp_language_codes(apps, schema_editor):
    """
    Best-effort reverse, using the same rules as the forward pass.

    This does not restore the state the forward pass ran against. Rows whose video already
    held the canonical code were deleted on the way forward, and a rollback cannot bring them
    back -- a video that had both "es" and "es-419" comes out of the round trip with only
    "es". Lossy in two further ways: "es-419" reverses to "es" and the es-419/es-es
    distinction is gone, and rows that legitimately held an edX code before this migration
    ever ran are rewritten to the GCP spelling along with everything else.
    """
    VideoTranscript = apps.get_model("edxval", "VideoTranscript")
    db_alias = schema_editor.connection.alias

    for edx_code, gcp_code in EDX_TO_GCP.items():
        _backfill_language_code(VideoTranscript, db_alias, edx_code, gcp_code)


class Migration(migrations.Migration):

    dependencies = [
        ('edxval', '0005_videoaudiodescription'),
    ]

    operations = [
        migrations.RunPython(gcp_to_edx_language_codes, reverse_code=edx_to_gcp_language_codes),
    ]
