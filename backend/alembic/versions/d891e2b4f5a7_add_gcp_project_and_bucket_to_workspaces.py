# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Add GCP Project and Bucket to Workspaces

Revision ID: d891e2b4f5a7
Revises: cb3c4680571b
Create Date: 2026-09-02 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d891e2b4f5a7"
down_revision: Union[str, None] = "cb3c4680571b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("gcp_project_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("gcs_bucket_name", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "gcs_bucket_name")
    op.drop_column("workspaces", "gcp_project_id")
