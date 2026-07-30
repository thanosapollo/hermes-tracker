"""Tests for the global label palette catalog endpoint."""

from __future__ import annotations

from conftest import AUTH_HEADERS
from hermes_tracker.domain import ESTABLISHED_LABEL_COLORS, UNKNOWN_LABEL_COLOR


class TestLabelCatalogApi:
    def test_unauthenticated_labels_401(self, client) -> None:
        assert client.get("/api/v1/labels").status_code == 401

    def test_label_catalog_returns_established_colors(self, client) -> None:
        resp = client.get("/api/v1/labels", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_color"] == UNKNOWN_LABEL_COLOR
        assert body["fallback_color"] == "#9a8e78"
        names = [entry["name"] for entry in body["labels"]]
        assert names == sorted(ESTABLISHED_LABEL_COLORS)
        assert {entry["name"]: entry["color"] for entry in body["labels"]} == (
            ESTABLISHED_LABEL_COLORS
        )
        assert all(
            entry["color"].startswith("#") and len(entry["color"]) == 7 for entry in body["labels"]
        )
