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
"""Tests for CreateWorkspaceDto and UpdateWorkspaceGcpConfigDto validation."""

import pytest
from pydantic import ValidationError

from src.workspaces.dto.create_workspace_dto import (
    CreateWorkspaceDto,
    UpdateWorkspaceGcpConfigDto,
)


class TestCreateWorkspaceDto:
    """Tests for workspace DTO validators."""

    def test_valid_fields(self):
        dto = CreateWorkspaceDto(
            name="My Show",
            gcp_project_id="my-project-123",
            gcs_bucket_name="gs://my-custom-bucket",
        )
        assert dto.name == "My Show"
        assert dto.gcp_project_id == "my-project-123"
        assert dto.gcs_bucket_name == "my-custom-bucket"

    def test_empty_string_normalized_to_none(self):
        dto = CreateWorkspaceDto(
            name="My Show",
            gcp_project_id="   ",
            gcs_bucket_name="   ",
        )
        assert dto.gcp_project_id is None
        assert dto.gcs_bucket_name is None

    def test_invalid_project_id_raises(self):
        with pytest.raises(ValidationError) as exc:
            CreateWorkspaceDto(
                name="My Show",
                gcp_project_id="-invalid-start",
            )
        assert "Invalid GCP Project ID format" in str(exc.value)

    def test_invalid_bucket_name_raises(self):
        with pytest.raises(ValidationError) as exc:
            CreateWorkspaceDto(
                name="My Show",
                gcs_bucket_name="!invalid!",
            )
        assert "Invalid GCS Bucket name format" in str(exc.value)


class TestUpdateWorkspaceGcpConfigDto:
    """Tests for UpdateWorkspaceGcpConfigDto validators."""

    def test_valid_update(self):
        dto = UpdateWorkspaceGcpConfigDto(
            gcp_project_id="valid-project-id",
            gcs_bucket_name="gs://valid-bucket-name",
        )
        assert dto.gcp_project_id == "valid-project-id"
        assert dto.gcs_bucket_name == "valid-bucket-name"

    def test_empty_string_normalized(self):
        dto = UpdateWorkspaceGcpConfigDto(
            gcp_project_id=" ",
            gcs_bucket_name=" ",
        )
        assert dto.gcp_project_id is None
        assert dto.gcs_bucket_name is None

    def test_invalid_project_raises(self):
        with pytest.raises(ValidationError) as exc:
            UpdateWorkspaceGcpConfigDto(gcp_project_id="AB")
        assert "Invalid GCP Project ID format" in str(exc.value)

    def test_invalid_bucket_raises(self):
        with pytest.raises(ValidationError) as exc:
            UpdateWorkspaceGcpConfigDto(gcs_bucket_name="?#$%")
        assert "Invalid GCS Bucket name format" in str(exc.value)
