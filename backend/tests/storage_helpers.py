"""Destructive teardown is restricted to isolated test buckets and test credentials."""


def clear_test_bucket(store):
    bucket = store.settings.s3_bucket
    assert store.settings.environment == "test"
    assert bucket.startswith("arclight-test-")
    assert store.settings.s3_access_key.get_secret_value() == "arclight-test"
    for page in store.client.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for row in page.get("Versions", []) + page.get("DeleteMarkers", []):
            store.client.delete_object(Bucket=bucket, Key=row["Key"], VersionId=row["VersionId"])
    store.client.delete_bucket(Bucket=bucket)
