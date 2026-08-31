from unittest.mock import MagicMock

import pytest

import s3_utils


def test_cdn_invalidation_is_not_needed_without_a_cdn_domain(monkeypatch):
    monkeypatch.setattr(s3_utils.settings, "AWS_CDN_DOMAIN", "")

    assert s3_utils.invalidate_cdn_object("space-market/resources/r1/content.json") is False


def test_cdn_invalidation_requires_a_distribution_id(monkeypatch):
    monkeypatch.setattr(s3_utils.settings, "AWS_CDN_DOMAIN", "cdn.example.test")
    monkeypatch.setattr(s3_utils.settings, "AWS_CLOUDFRONT_DISTRIBUTION_ID", "")

    with pytest.raises(RuntimeError, match="AWS_CLOUDFRONT_DISTRIBUTION_ID"):
        s3_utils.invalidate_cdn_object("space-market/resources/r1/content.json")


def test_cdn_invalidation_targets_the_exact_object_path(monkeypatch):
    cloudfront = MagicMock()
    monkeypatch.setattr(s3_utils.settings, "AWS_CDN_DOMAIN", "cdn.example.test")
    monkeypatch.setattr(s3_utils.settings, "AWS_CLOUDFRONT_DISTRIBUTION_ID", "DIST123")
    monkeypatch.setattr(s3_utils.boto3, "client", lambda *_args, **_kwargs: cloudfront)

    assert s3_utils.invalidate_cdn_object("space-market/resources/r1/content.json") is True

    call = cloudfront.create_invalidation.call_args.kwargs
    assert call["DistributionId"] == "DIST123"
    assert call["InvalidationBatch"]["Paths"] == {
        "Quantity": 1,
        "Items": ["/space-market/resources/r1/content.json"],
    }
    assert call["InvalidationBatch"]["CallerReference"].startswith("space-market-")
