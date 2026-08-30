"""clear missing or unowned Minecraft character skins

Revision ID: f2c9d10a4b7e
Revises: e7f4a91c2b6d
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f2c9d10a4b7e"
down_revision: Union[str, None] = "e7f4a91c2b6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep a character skin only when its stored key or URL resolves to an
    # undeleted GenerationLog owned by the same user.  The suffix comparison
    # covers CDN/S3 URLs while split_part removes signed-URL query/fragment
    # data.  Legacy URLs with no matching log are intentionally cleared.
    op.execute(
        """
        UPDATE users AS user_account
        SET minecraft_skin_url = NULL
        WHERE user_account.minecraft_skin_url IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM generation_logs AS generation_log
              CROSS JOIN LATERAL (
                  VALUES
                      (generation_log.result),
                      (generation_log.edited_result),
                      (generation_log.image_to_skin_edited_result)
              ) AS candidate(skin_ref)
              WHERE generation_log.user_id = user_account.id
                AND generation_log.is_deleted IS NOT TRUE
                AND candidate.skin_ref IS NOT NULL
                AND (
                    user_account.minecraft_skin_url = candidate.skin_ref
                    OR split_part(
                        split_part(user_account.minecraft_skin_url, '?', 1),
                        '#',
                        1
                    ) = split_part(
                        split_part(candidate.skin_ref, '?', 1),
                        '#',
                        1
                    )
                    OR right(
                        split_part(
                            split_part(user_account.minecraft_skin_url, '?', 1),
                            '#',
                            1
                        ),
                        char_length(
                            split_part(
                                split_part(candidate.skin_ref, '?', 1),
                                '#',
                                1
                            )
                        ) + 1
                    ) = '/' || split_part(
                        split_part(candidate.skin_ref, '?', 1),
                        '#',
                        1
                    )
                )
          )
        """
    )


def downgrade() -> None:
    # Cleared URLs cannot be reconstructed safely.
    pass
