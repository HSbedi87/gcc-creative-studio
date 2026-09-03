# Copyright 2025 Google LLC
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

import re
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

GCP_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
GCS_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


class CreateWorkspaceDto(BaseModel):
    """Data transfer object for creating a new workspace."""

    name: str = Field(..., min_length=3, max_length=100)
    gcp_project_id: str | None = Field(
        default=None,
        description="Optional GCP Project ID for routing API calls and billing.",
    )
    gcs_bucket_name: str | None = Field(
        default=None,
        description="Optional GCS Bucket name for storing workspace media.",
    )

    @field_validator("gcp_project_id")
    @classmethod
    def validate_gcp_project_id(cls, v: str | None) -> str | None:
        if v is not None:
            v_clean = v.strip()
            if not v_clean:
                return None
            if not GCP_PROJECT_ID_PATTERN.match(v_clean):
                raise ValueError(
                    "Invalid GCP Project ID format. Must be 6-30 characters, lowercase letters, numbers, and hyphens, starting with a letter."
                )
            return v_clean
        return None

    @field_validator("gcs_bucket_name")
    @classmethod
    def validate_gcs_bucket_name(cls, v: str | None) -> str | None:
        if v is not None:
            v_clean = v.strip()
            if not v_clean:
                return None
            # Strip gs:// prefix if user entered it
            if v_clean.startswith("gs://"):
                v_clean = v_clean[5:]
            if not GCS_BUCKET_PATTERN.match(v_clean):
                raise ValueError(
                    "Invalid GCS Bucket name format. Must be 3-63 characters, lowercase letters, numbers, hyphens, underscores, or dots."
                )
            return v_clean
        return None

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class UpdateWorkspaceGcpConfigDto(BaseModel):
    """Data transfer object for updating a workspace's GCP project and bucket configuration."""

    gcp_project_id: str | None = Field(
        default=None,
        description="GCP Project ID to route Vertex AI / API billing to.",
    )
    gcs_bucket_name: str | None = Field(
        default=None,
        description="Custom GCS bucket to store generated assets in.",
    )

    @field_validator("gcp_project_id")
    @classmethod
    def validate_gcp_project_id(cls, v: str | None) -> str | None:
        if v is not None:
            v_clean = v.strip()
            if not v_clean:
                return None
            if not GCP_PROJECT_ID_PATTERN.match(v_clean):
                raise ValueError(
                    "Invalid GCP Project ID format. Must be 6-30 characters, lowercase letters, numbers, and hyphens, starting with a letter."
                )
            return v_clean
        return None

    @field_validator("gcs_bucket_name")
    @classmethod
    def validate_gcs_bucket_name(cls, v: str | None) -> str | None:
        if v is not None:
            v_clean = v.strip()
            if not v_clean:
                return None
            if v_clean.startswith("gs://"):
                v_clean = v_clean[5:]
            if not GCS_BUCKET_PATTERN.match(v_clean):
                raise ValueError(
                    "Invalid GCS Bucket name format. Must be 3-63 characters, lowercase letters, numbers, hyphens, underscores, or dots."
                )
            return v_clean
        return None

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)
